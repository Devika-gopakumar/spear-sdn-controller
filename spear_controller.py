# =============================================================================
#  SPEAR CONTROLLER  —  spear_controller.py
#  OpenFlow 1.3  |  Ryu SDN Framework
#  Strategy: Flow-Mod Installation + Proactive Elephant Separation
#
# ─────────────────────────────────────────────────────────────────────────────
#  ROOT-CAUSE FIX SUMMARY (v3 — pingall 100% drop fixed)
# ─────────────────────────────────────────────────────────────────────────────
#
#  FIX-ARP   ARP proxy replaces simple flood.
#            Problem: multi-hop topology — flood on ingress switch only causes
#            cascade of controller hits; ARP reply often lost → pingall drops.
#            Fix: controller maintains host_ip_to_mac table.
#              • ARP REQUEST + target known  → synthetic ARP REPLY sent back
#                directly. Zero flooding needed, zero timing dependency.
#              • ARP REQUEST + target unknown → flood normally; table-miss
#                cascade propagates it; reply teaches us the target MAC.
#              • ARP REPLY → learn src, unicast to target's known port.
#
#  FIX-BARRIER  OFPBarrierRequest after table-miss install.
#               Without this, a race condition allows data packets to arrive
#               before the switch commits the table-miss rule → silent drop.
#
#  FIX-PKTOUT  PacketOut data= only when buffer_id == OFP_NO_BUFFER.
#              Sending data= with a valid buffer_id → OFPBRC_BUFFER_UNKNOWN
#              → switch drops packet. Applied to ALL PacketOut calls.
#
#  FIX-CACHE   Mouse path cache key = (dpid, src_ip, dst_ip).
#              Old key (src_ip, dst_ip) returned wrong path for reverse traffic.
#
#  FIX-ABORT   _install_path pre-checks all DPIDs registered before installing
#              any rule. Half-installed paths → black-holes.
#
#  FIX-PROACT  Elephant Dijkstra uses mouse-boosted graph copy (not self.graph).
#              Per-link mouse count × MOUSE_LINK_PENALTY added before elephant
#              path selection → elephant avoids loaded paths proactively,
#              before PUI threshold fires.
#
#  FIX-TIEBRK Dijkstra tie-breaks by lower DPID → deterministic paths.
#
#  FIX-DELPRI  _delete_path_rules takes priority as argument.
#              DELETE_STRICT must match exact priority used at install time.
#
# ─────────────────────────────────────────────────────────────────────────────
#  PORT MAP  (matches topology.py addLink order exactly)
# ─────────────────────────────────────────────────────────────────────────────
#   Link      SW-A port   SW-B port
#   h1—s1     —           s1:1
#   s1—s2     s1:2        s2:1
#   s1—s3     s1:3        s3:1
#   s1—s5     s1:4        s5:1
#   s2—s4     s2:2        s4:1
#   s3—s4     s3:2        s4:2
#   s5—s6     s5:2        s6:1
#   s6—s4     s6:2        s4:3
#   s4—h2     s4:4        —
# =============================================================================

import copy

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, MAIN_DISPATCHER,
                                     DEAD_DISPATCHER, set_ev_cls)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, tcp
from ryu.lib.packet import arp as arp_lib
from ryu.lib.packet import packet as pkt_lib
from ryu.lib import hub

# =============================================================================
#  TUNABLE CONSTANTS
# =============================================================================
CONGESTION_THRESHOLD_MBPS = 50
GHOST_WEIGHT_PENALTY      = 1000
GHOST_WEIGHT_MAX          = 2000
DAMPING_STEP              = 200
MONITOR_INTERVAL_SEC      = 5

FLOW_IDLE_TIMEOUT         = 30
FLOW_HARD_TIMEOUT         = 120

ELEPHANT_PORT             = 5001
ELEPHANT_PRIORITY         = 200
MOUSE_PRIORITY            = 100
TABLE_MISS_PRIORITY       = 0

MOUSE_LINK_PENALTY        = 300   # weight boost per active mouse on a link


# =============================================================================
class SpearController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SpearController, self).__init__(*args, **kwargs)

        self.datapaths        = {}
        self.port_history     = {}
        self.mac_to_port      = {}   # dpid → {mac → port}
        self.elephant_flows   = {}   # (sip,dip,sp,dp) → {path,smac,dmac}
        self.mouse_path_cache = {}   # (dpid,sip,dip) → path
        self.mouse_flows      = {}   # (u,v) → active mouse count

        # ARP proxy tables  [FIX-ARP]
        self.host_ip_to_mac   = {}   # ip  → mac
        self.host_mac_to_sw   = {}   # mac → (dpid, port)

        # ── Topology graph ────────────────────────────────────────────────────
        self.graph = {
            1: {2: 1, 3: 1, 5: 1},
            2: {1: 1, 4: 1},
            3: {1: 1, 4: 1},
            4: {2: 1, 3: 1, 6: 1},
            5: {1: 1, 6: 1},
            6: {5: 1, 4: 1},
        }

        # ── Physical port map ─────────────────────────────────────────────────
        self.link_to_port = {
            (1, 2): 2,  (1, 3): 3,  (1, 5): 4,
            (2, 1): 1,  (2, 4): 2,
            (3, 1): 1,  (3, 4): 2,
            (5, 1): 1,  (5, 6): 2,
            (6, 5): 1,  (6, 4): 2,
            (4, 2): 1,  (4, 3): 2,  (4, 6): 3,
        }

        self.host_port = {1: 1, 4: 4}
        self.DST_SW    = 4
        self.SRC_SW    = 1

        self.monitor_thread = hub.spawn(self._monitor)

        self.logger.info("=" * 66)
        self.logger.info("  SPEAR CONTROLLER v3  (ARP proxy + proactive elephant)")
        self.logger.info("  Elephant port      : TCP %d", ELEPHANT_PORT)
        self.logger.info("  Congestion trigger : %.0f Mbps", CONGESTION_THRESHOLD_MBPS)
        self.logger.info("  Ghost weight max   : %d  penalty=%d  damping=%d",
                         GHOST_WEIGHT_MAX, GHOST_WEIGHT_PENALTY, DAMPING_STEP)
        self.logger.info("  Mouse link penalty : %d per active mouse", MOUSE_LINK_PENALTY)
        self.logger.info("  Flow timeouts      : idle=%ds  hard=%ds",
                         FLOW_IDLE_TIMEOUT, FLOW_HARD_TIMEOUT)
        self.logger.info("=" * 66)

    # =========================================================================
    #  SWITCH HANDSHAKE  [FIX-BARRIER]
    # =========================================================================
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        dpid     = datapath.id
        ofp      = datapath.ofproto
        parser   = datapath.ofproto_parser

        self.datapaths[dpid] = datapath
        self.mac_to_port.setdefault(dpid, {})
        self.logger.info("[HANDSHAKE] S%d connected", dpid)

        # 1. Flush all stale rules
        datapath.send_msg(parser.OFPFlowMod(
            datapath  = datapath,
            command   = ofp.OFPFC_DELETE,
            out_port  = ofp.OFPP_ANY,
            out_group = ofp.OFPG_ANY,
            match     = parser.OFPMatch(),
        ))

        # 2. Table-miss: send full packet to controller
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst    = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        datapath.send_msg(parser.OFPFlowMod(
            datapath     = datapath,
            priority     = TABLE_MISS_PRIORITY,
            match        = parser.OFPMatch(),
            instructions = inst,
        ))

        # 3. Barrier — ensure switch commits rules before data arrives [FIX-BARRIER]
        datapath.send_msg(parser.OFPBarrierRequest(datapath))
        self.logger.info("[HANDSHAKE] S%d table-miss + barrier sent", dpid)

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def _state_change_handler(self, ev):
        dpid = ev.datapath.id
        if dpid in self.datapaths:
            del self.datapaths[dpid]
            self.logger.warning("[DISCONNECT] S%d removed", dpid)

    # =========================================================================
    #  DIJKSTRA  [FIX-TIEBRK]
    # =========================================================================
    def get_optimal_path(self, start, goal, graph=None):
        g = graph if graph is not None else self.graph

        if start == goal:
            return [goal]
        if start not in g or goal not in g:
            self.logger.error("[DIJKSTRA] Node %s or %s not in graph", start, goal)
            return []

        dist = {n: float('inf') for n in g}
        prev = {n: None         for n in g}
        dist[start] = 0
        unvisited   = set(g.keys())

        while unvisited:
            u = min(unvisited, key=lambda n: (dist[n], n))  # [FIX-TIEBRK]
            if dist[u] == float('inf'):
                break
            unvisited.discard(u)
            for v, w in g[u].items():
                alt = dist[u] + w
                if alt < dist[v]:
                    dist[v] = alt
                    prev[v]  = u

        path, node = [], goal
        while node is not None:
            path.insert(0, node)
            node = prev[node]

        if not path or path[0] != start:
            self.logger.warning("[DIJKSTRA] No path %s→%s", start, goal)
            return []

        self.logger.debug("[DIJKSTRA] %s→%s  path=%s", start, goal, path)
        return path

    # =========================================================================
    #  MOUSE-BOOSTED GRAPH  [FIX-PROACT]
    # =========================================================================
    def _build_mouse_boosted_graph(self):
        boosted = copy.deepcopy(self.graph)
        for (u, v), count in self.mouse_flows.items():
            if count > 0 and u in boosted and v in boosted[u]:
                boosted[u][v] += count * MOUSE_LINK_PENALTY
        return boosted

    def _track_mouse_path(self, path, delta):
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            self.mouse_flows[(u, v)] = max(0,
                self.mouse_flows.get((u, v), 0) + delta)

    # =========================================================================
    #  ARP PROXY  [FIX-ARP]
    # =========================================================================
    def _handle_arp(self, datapath, in_port, raw_data, eth_pkt, arp_pkt):
        ofp    = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid   = datapath.id

        src_mac = arp_pkt.src_mac
        dst_mac = arp_pkt.dst_mac
        src_ip  = arp_pkt.src_ip
        dst_ip  = arp_pkt.dst_ip

        # Learn the sender
        self.host_ip_to_mac[src_ip]  = src_mac
        self.host_mac_to_sw[src_mac] = (dpid, in_port)
        self.mac_to_port.setdefault(dpid, {})[src_mac] = in_port

        self.logger.debug("[ARP] S%d P%d  %s(%s)→%s  op=%d",
                          dpid, in_port, src_ip, src_mac, dst_ip, arp_pkt.opcode)

        if arp_pkt.opcode == arp_lib.ARP_REQUEST:
            if dst_ip in self.host_ip_to_mac:
                # Target known — send synthetic reply directly back
                reply_mac = self.host_ip_to_mac[dst_ip]
                self._send_arp_reply(datapath, in_port,
                                     reply_mac, dst_ip,
                                     src_mac,  src_ip)
                self.logger.info("[ARP PROXY] %s→%s replied via S%d P%d",
                                 dst_ip, src_ip, dpid, in_port)
            else:
                # Unknown — flood; table-miss cascade will propagate
                self._do_packet_out(datapath, ofp.OFP_NO_BUFFER,
                                    in_port, ofp.OFPP_FLOOD, raw_data)
                self.logger.info("[ARP FLOOD] Unknown %s — flood from S%d", dst_ip, dpid)

        else:  # ARP_REPLY
            self.host_ip_to_mac[dst_ip] = dst_mac
            if dst_mac in self.host_mac_to_sw:
                tgt_dpid, tgt_port = self.host_mac_to_sw[dst_mac]
                if tgt_dpid == dpid:
                    self._do_packet_out(datapath, ofp.OFP_NO_BUFFER,
                                        in_port, tgt_port, raw_data)
                else:
                    path = self.get_optimal_path(dpid, tgt_dpid)
                    out_port = (self.link_to_port.get((dpid, path[1]), ofp.OFPP_FLOOD)
                                if path and len(path) > 1 else ofp.OFPP_FLOOD)
                    self._do_packet_out(datapath, ofp.OFP_NO_BUFFER,
                                        in_port, out_port, raw_data)
            else:
                self._do_packet_out(datapath, ofp.OFP_NO_BUFFER,
                                    in_port, ofp.OFPP_FLOOD, raw_data)

    def _send_arp_reply(self, datapath, out_port,
                        src_mac, src_ip, dst_mac, dst_ip):
        """Craft synthetic ARP REPLY and send it."""
        parser = datapath.ofproto_parser
        ofp    = datapath.ofproto

        e = ethernet.ethernet(dst=dst_mac, src=src_mac, ethertype=0x0806)
        a = arp_lib.arp(opcode=arp_lib.ARP_REPLY,
                        src_mac=src_mac, src_ip=src_ip,
                        dst_mac=dst_mac, dst_ip=dst_ip)
        p = pkt_lib.Packet()
        p.add_protocol(e)
        p.add_protocol(a)
        p.serialize()

        datapath.send_msg(parser.OFPPacketOut(
            datapath  = datapath,
            buffer_id = ofp.OFP_NO_BUFFER,
            in_port   = ofp.OFPP_CONTROLLER,
            actions   = [parser.OFPActionOutput(out_port)],
            data      = p.data,
        ))

    def _do_packet_out(self, datapath, buffer_id, in_port, out_port, data):
        """[FIX-PKTOUT] Safe PacketOut: attach data only when not buffered."""
        parser   = datapath.ofproto_parser
        ofp      = datapath.ofproto
        pkt_data = data if buffer_id == ofp.OFP_NO_BUFFER else None
        datapath.send_msg(parser.OFPPacketOut(
            datapath  = datapath,
            buffer_id = buffer_id,
            in_port   = in_port,
            actions   = [parser.OFPActionOutput(out_port)],
            data      = pkt_data,
        ))

    # =========================================================================
    #  MONITORING THREAD
    # =========================================================================
    def _monitor(self):
        while True:
            hub.sleep(MONITOR_INTERVAL_SEC)

            for u in list(self.graph):
                for v in list(self.graph[u]):
                    if self.graph[u][v] > 1:
                        self.graph[u][v] = max(1, self.graph[u][v] - DAMPING_STEP)

            elevated = {(u, v): self.graph[u][v]
                        for u in self.graph for v in self.graph[u]
                        if self.graph[u][v] > 1}
            if elevated:
                self.logger.info("[DAMPING] %s", elevated)

            for dp in list(self.datapaths.values()):
                self._request_port_stats(dp)

    def _request_port_stats(self, datapath):
        parser = datapath.ofproto_parser
        ofp    = datapath.ofproto
        datapath.send_msg(parser.OFPPortStatsRequest(datapath, 0, ofp.OFPP_ANY))

    # =========================================================================
    #  PORT STATS → CONGESTION
    # =========================================================================
    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid      = ev.msg.datapath.id
        congested = False

        for stat in ev.msg.body:
            if stat.port_no >= ofproto_v1_3.OFPP_MAX:
                continue

            key = (dpid, stat.port_no)
            prev_bytes, prev_speed = self.port_history.get(key, (stat.tx_bytes, 0.0))
            delta = stat.tx_bytes - prev_bytes
            speed = (delta * 8) / MONITOR_INTERVAL_SEC / 1e6
            accel = (speed - prev_speed) / MONITOR_INTERVAL_SEC
            pui   = speed + 0.5 * accel
            self.port_history[key] = (stat.tx_bytes, speed)

            self.logger.debug("[STATS] S%d P%d  %.2f Mbps  PUI=%.2f",
                              dpid, stat.port_no, speed, pui)

            if pui > CONGESTION_THRESHOLD_MBPS:
                self.logger.warning("[CONGESTION] S%d P%d  PUI=%.2f Mbps",
                                    dpid, stat.port_no, pui)
                for (u, v), port in self.link_to_port.items():
                    if u == dpid and port == stat.port_no:
                        old_w = self.graph[u][v]
                        self.graph[u][v] = min(old_w + GHOST_WEIGHT_PENALTY,
                                               GHOST_WEIGHT_MAX)
                        self.logger.warning("[GHOST] S%d→S%d  %d→%d",
                                            u, v, old_w, self.graph[u][v])
                        congested = True

        if congested:
            self._reroute_elephants()

    # =========================================================================
    #  ELEPHANT REROUTING
    # =========================================================================
    def _reroute_elephants(self):
        if not self.elephant_flows:
            return
        self.logger.info("[REROUTE] %d elephant(s)", len(self.elephant_flows))
        boosted = self._build_mouse_boosted_graph()

        for flow_key, info in list(self.elephant_flows.items()):
            src_ip, dst_ip, src_port, dst_port = flow_key
            old_path = info['path']
            src_mac  = info['src_mac']
            dst_mac  = info['dst_mac']

            new_path = self.get_optimal_path(old_path[0], self.DST_SW, graph=boosted)
            if not new_path or new_path == old_path:
                continue

            self.logger.info("[REROUTE] %s:%d→%s:%d  %s → %s",
                             src_ip, src_port, dst_ip, dst_port, old_path, new_path)
            self._delete_path_rules(
                old_path, src_mac, dst_mac,
                src_ip, dst_ip, src_port, dst_port,
                is_tcp=True, priority=ELEPHANT_PRIORITY
            )
            self._install_path(
                new_path, src_mac, dst_mac,
                src_ip, dst_ip, src_port, dst_port,
                priority=ELEPHANT_PRIORITY, is_elephant=True
            )

        self.mouse_path_cache.clear()

    # =========================================================================
    #  DELETE PATH RULES  [FIX-DELPRI]
    # =========================================================================
    def _delete_path_rules(self, path, src_mac, dst_mac,
                           src_ip, dst_ip, src_port, dst_port,
                           is_tcp, priority):
        for i, dpid in enumerate(path):
            if dpid not in self.datapaths:
                continue
            datapath = self.datapaths[dpid]
            parser   = datapath.ofproto_parser
            ofp      = datapath.ofproto

            fwd_in = (self.host_port.get(dpid, ofp.OFPP_ANY)
                      if i == 0
                      else self.link_to_port.get((dpid, path[i-1]), ofp.OFPP_ANY))
            rev_in = (self.host_port.get(dpid, ofp.OFPP_ANY)
                      if i == len(path) - 1
                      else self.link_to_port.get((dpid, path[i+1]), ofp.OFPP_ANY))

            for (mi, msrc, mdst, msip, mdip, msp, mdp) in [
                (fwd_in, src_mac, dst_mac, src_ip, dst_ip, src_port, dst_port),
                (rev_in, dst_mac, src_mac, dst_ip, src_ip, dst_port, src_port),
            ]:
                match = self._build_match(parser, mi, msrc, mdst,
                                          msip, mdip, msp, mdp, is_tcp)
                datapath.send_msg(parser.OFPFlowMod(
                    datapath  = datapath,
                    command   = ofp.OFPFC_DELETE_STRICT,
                    priority  = priority,
                    out_port  = ofp.OFPP_ANY,
                    out_group = ofp.OFPG_ANY,
                    match     = match,
                ))

    # =========================================================================
    #  INSTALL PATH  [FIX-ABORT]
    # =========================================================================
    def _install_path(self, path, src_mac, dst_mac,
                      src_ip, dst_ip, src_port, dst_port,
                      priority=MOUSE_PRIORITY, is_elephant=False):

        # Pre-flight: all switches must be registered  [FIX-ABORT]
        for dpid in path:
            if dpid not in self.datapaths:
                self.logger.warning("[INSTALL ABORT] S%d missing — skip %s", dpid, path)
                return

        is_tcp = (src_port != 0 or dst_port != 0)

        for i, dpid in enumerate(path):
            datapath = self.datapaths[dpid]
            parser   = datapath.ofproto_parser
            ofp      = datapath.ofproto

            fwd_in  = (self.host_port.get(dpid, ofp.OFPP_ANY)
                       if i == 0
                       else self.link_to_port.get((dpid, path[i-1]), ofp.OFPP_ANY))
            fwd_out = (self.host_port.get(dpid, ofp.OFPP_FLOOD)
                       if dpid == path[-1]
                       else self.link_to_port.get((dpid, path[i+1]), ofp.OFPP_FLOOD))
            rev_in  = (self.host_port.get(dpid, ofp.OFPP_ANY)
                       if i == len(path) - 1
                       else self.link_to_port.get((dpid, path[i+1]), ofp.OFPP_ANY))
            rev_out = (self.host_port.get(dpid, ofp.OFPP_FLOOD)
                       if dpid == path[0]
                       else self.link_to_port.get((dpid, path[i-1]), ofp.OFPP_FLOOD))

            fwd_m = self._build_match(parser, fwd_in, src_mac, dst_mac,
                                      src_ip, dst_ip, src_port, dst_port, is_tcp)
            self._send_flow_mod(datapath, priority, fwd_m,
                                [parser.OFPActionOutput(fwd_out)])

            rev_m = self._build_match(parser, rev_in, dst_mac, src_mac,
                                      dst_ip, src_ip, dst_port, src_port, is_tcp)
            self._send_flow_mod(datapath, priority, rev_m,
                                [parser.OFPActionOutput(rev_out)])

            self.logger.debug(
                "[INSTALL] S%d  FWD in=%d→out=%d | REV in=%d→out=%d",
                dpid, fwd_in, fwd_out, rev_in, rev_out
            )

        if is_elephant:
            key = (src_ip, dst_ip, src_port, dst_port)
            self.elephant_flows[key] = {
                'path'   : path,
                'src_mac': src_mac,
                'dst_mac': dst_mac,
            }
            self.logger.info("[ELEPHANT] %s:%d→%s:%d  path=%s",
                             src_ip, src_port, dst_ip, dst_port, path)
        else:
            self._track_mouse_path(path, +1)
            self.logger.debug("[MOUSE] path=%s  counts=%s", path, self.mouse_flows)

    # =========================================================================
    #  MATCH BUILDER
    # =========================================================================
    def _build_match(self, parser, in_port, src_mac, dst_mac,
                     src_ip, dst_ip, src_port, dst_port, is_tcp):
        if is_tcp:
            return parser.OFPMatch(
                in_port  = in_port,
                eth_type = 0x0800,
                eth_src  = src_mac,
                eth_dst  = dst_mac,
                ipv4_src = src_ip,
                ipv4_dst = dst_ip,
                ip_proto = 6,
                tcp_src  = src_port,
                tcp_dst  = dst_port,
            )
        return parser.OFPMatch(
            in_port  = in_port,
            eth_type = 0x0800,
            eth_src  = src_mac,
            eth_dst  = dst_mac,
            ipv4_src = src_ip,
            ipv4_dst = dst_ip,
        )

    # =========================================================================
    #  FLOWMOD HELPER
    # =========================================================================
    def _send_flow_mod(self, datapath, priority, match, actions):
        parser = datapath.ofproto_parser
        ofp    = datapath.ofproto
        inst   = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        datapath.send_msg(parser.OFPFlowMod(
            datapath     = datapath,
            command      = ofp.OFPFC_ADD,
            priority     = priority,
            idle_timeout = FLOW_IDLE_TIMEOUT,
            hard_timeout = FLOW_HARD_TIMEOUT,
            flags        = ofp.OFPFF_SEND_FLOW_REM,
            match        = match,
            instructions = inst,
        ))

    # =========================================================================
    #  FLOW REMOVED
    # =========================================================================
    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed_handler(self, ev):
        msg   = ev.msg
        match = msg.match
        proto = match.get('ip_proto')

        if proto == 6:
            key = (match.get('ipv4_src', ''), match.get('ipv4_dst', ''),
                   match.get('tcp_src', 0),   match.get('tcp_dst', 0))
            if key in self.elephant_flows:
                del self.elephant_flows[key]
                self.logger.info("[EXPIRED] Elephant %s:%s→%s:%s",
                                 key[0], key[2], key[1], key[3])
        else:
            src_ip = match.get('ipv4_src', '')
            dst_ip = match.get('ipv4_dst', '')
            for ck, path in list(self.mouse_path_cache.items()):
                if len(ck) == 3 and ck[1] == src_ip and ck[2] == dst_ip:
                    self._track_mouse_path(path, -1)
                    break

    # =========================================================================
    #  PACKET-IN HANDLER
    # =========================================================================
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg      = ev.msg
        datapath = msg.datapath
        dpid     = datapath.id
        ofp      = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match['in_port']

        pkt     = packet.Packet(msg.data)
        eth     = pkt.get_protocols(ethernet.ethernet)[0]

        # Drop IPv6 / multicast
        if eth.ethertype == 0x86DD:
            return
        if eth.dst.startswith('33:33') or eth.dst.startswith('01:00:5e'):
            return

        # MAC learning
        self.mac_to_port.setdefault(dpid, {})[eth.src] = in_port

        # ── ARP  [FIX-ARP] ───────────────────────────────────────────────────
        if eth.ethertype == 0x0806:
            arp_pkt = pkt.get_protocol(arp_lib.arp)
            if arp_pkt:
                self._handle_arp(datapath, in_port, msg.data, eth, arp_pkt)
            return

        # IPv4 only
        if eth.ethertype != 0x0800:
            return
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt is None:
            return

        src_ip   = ip_pkt.src
        dst_ip   = ip_pkt.dst
        tcp_pkt  = pkt.get_protocol(tcp.tcp)
        src_port = tcp_pkt.src_port if tcp_pkt else 0
        dst_port = tcp_pkt.dst_port if tcp_pkt else 0

        # ── Classification ────────────────────────────────────────────────────
        is_elephant = (tcp_pkt is not None and
                       (src_port == ELEPHANT_PORT or dst_port == ELEPHANT_PORT))
        priority    = ELEPHANT_PRIORITY if is_elephant else MOUSE_PRIORITY

        # ── Path selection ────────────────────────────────────────────────────
        if is_elephant:
            boosted = self._build_mouse_boosted_graph()
            path    = self.get_optimal_path(dpid, self.DST_SW, graph=boosted)
            self.logger.info("[ELEPHANT] S%d boosted path→%s", dpid, path)
        else:
            cache_key = (dpid, src_ip, dst_ip)   # [FIX-CACHE]
            if cache_key not in self.mouse_path_cache:
                self.mouse_path_cache[cache_key] = \
                    self.get_optimal_path(dpid, self.DST_SW)
            path = self.mouse_path_cache[cache_key]

        if not path:
            self.logger.error("[DROP] No path S%d→S%d  %s→%s",
                              dpid, self.DST_SW, src_ip, dst_ip)
            return

        self.logger.info("[%s] S%d  %s:%d→%s:%d  path=%s",
                         "ELEPHANT" if is_elephant else "MOUSE",
                         dpid, src_ip, src_port, dst_ip, dst_port, path)

        # ── Install bidirectional rules ───────────────────────────────────────
        self._install_path(
            path, eth.src, eth.dst,
            src_ip, dst_ip, src_port, dst_port,
            priority=priority, is_elephant=is_elephant
        )

        # ── Forward trigger packet  [FIX-PKTOUT] ─────────────────────────────
        if len(path) > 1:
            out_port = self.link_to_port.get((dpid, path[1]), ofp.OFPP_FLOOD)
        elif dpid == self.DST_SW:
            out_port = self.host_port[self.DST_SW]
        else:
            out_port = ofp.OFPP_FLOOD

        self._do_packet_out(datapath, msg.buffer_id, in_port, out_port, msg.data)
