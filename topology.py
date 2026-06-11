# =============================================================================
#  TRIPLE-PATH TOPOLOGY  —  topology.py
#  Mininet custom topology for SPEAR SDN controller
#
#  Layout
#  ──────
#                     ┌──── s2 ────┐
#   h1 ──── s1 ───────┼──── s3 ────┼──── s4 ──── h2
#                     └── s5 ─ s6 ─┘
#
#  Path A : s1 → s2 → s4        (2 hops,  10 Mbps — narrow, congests first)
#  Path B : s1 → s3 → s4        (2 hops,  10 Mbps — narrow, congests first)
#  Path C : s1 → s5 → s6 → s4  (3 hops, 100 Mbps — wide fallback for elephants)
#
#  Port assignments  (controller hardcodes these in link_to_port / host_port)
#  ──────────────────────────────────────────────────────────────────────────
#   addLink call         Switch-A port   Switch-B port
#   addLink(h1, s1)      —               s1 : 1
#   addLink(s1, s2)      s1 : 2          s2 : 1
#   addLink(s1, s3)      s1 : 3          s3 : 1
#   addLink(s1, s5)      s1 : 4          s5 : 1
#   addLink(s2, s4)      s2 : 2          s4 : 1
#   addLink(s3, s4)      s3 : 2          s4 : 2
#   addLink(s5, s6)      s5 : 2          s6 : 1
#   addLink(s6, s4)      s6 : 2          s4 : 3
#   addLink(s4, h2)      s4 : 4          —
#
#  *** The order of addLink() calls is critical. ***
#  *** Never reorder them or port numbers change and the controller breaks. ***
#
#  Bandwidth design
#  ────────────────
#  Path A & B links : 10 Mbps  — congests immediately under iperf load
#  Path C links     : 100 Mbps — wide enough to absorb rerouted elephant
#  Host links       : 1000 Mbps — no bottleneck at the edge
#
#  Delay design
#  ────────────
#  Path A & B : 5 ms per link  → RTT ≈ 20 ms (2 hops × 2 × 5ms)
#  Path C     : 10 ms per link → RTT ≈ 60 ms (3 hops × 2 × 10ms)
#  Deliberately higher delay on Path C so mice prefer A/B and elephant
#  rerouting to C is visible in ping RTT when it happens.
#
#  How to run
#  ──────────
#  Terminal 1:
#    ryu-manager spear_controller.py --observe-links
#
#  Terminal 2 (wait for "S1…S6 connected" in Terminal 1):
#    sudo mn --custom topology.py \
#             --topo triple_path \
#             --controller remote,ip=127.0.0.1,port=6633 \
#             --switch ovsk,protocols=OpenFlow13 \
#             --link tc
#
#  NOTE: --link tc is required to honour bw= and delay= parameters below.
#        Without it Mininet ignores them and all links run at full speed.
#
#  Basic tests inside Mininet CLI:
#    mininet> pingall                          # should be 0% loss
#    mininet> h2 iperf -s -p 9999 &           # mouse server
#    mininet> h1 iperf -c h2 -p 9999 -t 30 & # mouse client
#    mininet> h2 iperf -s -p 5001 &           # elephant server
#    mininet> h1 iperf -c h2 -p 5001 -t 60   # elephant client
#    (watch Terminal 1 for ELEPHANT routing and REROUTE messages)
#
#  Verify flows installed on a switch:
#    mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
# =============================================================================

from mininet.topo  import Topo
from mininet.link  import TCLink


class TriplePathTopo(Topo):

    def build(self):

        # ── Hosts ─────────────────────────────────────────────────────────────
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')

        # ── Switches  (DPID forced to match integer index used in controller) ─
        s1 = self.addSwitch('s1', dpid='0000000000000001', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', dpid='0000000000000002', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', dpid='0000000000000003', protocols='OpenFlow13')
        s4 = self.addSwitch('s4', dpid='0000000000000004', protocols='OpenFlow13')
        s5 = self.addSwitch('s5', dpid='0000000000000005', protocols='OpenFlow13')
        s6 = self.addSwitch('s6', dpid='0000000000000006', protocols='OpenFlow13')

        # ── Links  (ORDER IS CRITICAL — determines port numbers) ──────────────

        # h1 side edge link  →  s1 gets port 1
        self.addLink(h1, s1,
                     bw=1000, delay='1ms')

        # Path A  (narrow — congests first under iperf)
        # s1—s2  →  s1:p2  s2:p1
        self.addLink(s1, s2, bw=10, delay='5ms')
        # s2—s4  →  s2:p2  s4:p1
        self.addLink(s2, s4, bw=10, delay='5ms')

        # Path B  (narrow — second to congest)
        # s1—s3  →  s1:p3  s3:p1
        self.addLink(s1, s3, bw=10, delay='5ms')
        # s3—s4  →  s3:p2  s4:p2
        self.addLink(s3, s4, bw=10, delay='5ms')

        # Path C  (wide — elephant fallback route)
        # s1—s5  →  s1:p4  s5:p1
        self.addLink(s1, s5, bw=100, delay='10ms')
        # s5—s6  →  s5:p2  s6:p1
        self.addLink(s5, s6, bw=100, delay='10ms')
        # s6—s4  →  s6:p2  s4:p3
        self.addLink(s6, s4, bw=100, delay='10ms')

        # h2 side edge link  →  s4 gets port 4  ← MUST be the last s4 link
        self.addLink(s4, h2,
                     bw=1000, delay='1ms')


# Registration string for --custom / --topo flags
topos = {'triple_path': (lambda: TriplePathTopo())}
