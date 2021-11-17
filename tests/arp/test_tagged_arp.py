
import pytest
import ptf.testutils as testutils

import itertools
import logging
import ipaddress
import pprint

from tests.common.fixtures.ptfhost_utils import change_mac_addresses        # lgtm[py/unused-import]
from tests.common.fixtures.duthost_utils import ports_list, utils_vlan_ports_list
from tests.common.helpers.assertions import pytest_require


logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('t0')
]

PTF_PORT_MAPPING_MODE = "use_orig_interface"
DUMMY_MAC_PREFIX = "02:11:22:33"
DUMMY_IP_PREFIX = "188.123"
DUMMY_ARP_COUNT = 10


@pytest.fixture(scope="module")
def skip_dualtor(tbinfo):
    """Skip running `test_tagged_arp` over dualtor."""
    pytest_require("dualtor" not in tbinfo["topo"]["name"], "Skip 'test_tagged_arp' over dualtor.")


@pytest.fixture(scope="module")
def cfg_facts(duthosts, rand_one_dut_hostname, skip_dualtor):
    duthost = duthosts[rand_one_dut_hostname]
    return duthost.config_facts(host=duthost.hostname, source="persistent")['ansible_facts']


def enable_arp(duthost, cfg_facts, enable):
    vlan_members = cfg_facts.get('VLAN_MEMBER', {})
    on_cmd = "echo 1 > /proc/sys/net/ipv4/conf/%s/arp_accept"
    off_cmd = "echo 0 > /proc/sys/net/ipv4/conf/%s/arp_accept"
    for vlan in vlan_members.keys():
        if enable:
            logger.info("Enable ARP for %s" % vlan)
            duthost.shell(on_cmd % vlan)
        else:
            logger.info("Disable ARP for %s" % vlan)
            duthost.shell(off_cmd % vlan)


def arp_cleanup(duthost):
    """ cleanup ARP entry """
    duthost.command('sonic-clear arp')


@pytest.fixture(scope="module", autouse=True)
def setup_arp(duthosts, rand_one_dut_hostname, cfg_facts):
    duthost = duthosts[rand_one_dut_hostname]
    # --------------------- Setup -----------------------
    try:
        enable_arp(duthost, cfg_facts, True)
    # --------------------- Testing -----------------------
        yield
    # --------------------- Teardown -----------------------
    finally:
        enable_arp(duthost, cfg_facts, False)
        arp_cleanup(duthost)


def build_arp_packet(vlan_id, neighbor_mac, dst_mac, neighbor_ip):

    pkt = testutils.simple_arp_packet(pktlen=60 if vlan_id == 0 else 64,
            eth_dst=dst_mac,
            eth_src=neighbor_mac,
            vlan_vid=vlan_id,
            arp_op=2,
            hw_snd=neighbor_mac,
            ip_snd=neighbor_ip,
            ip_tgt=neighbor_ip)
    return pkt

@pytest.mark.bsl
def test_tagged_arp_pkt(ptfadapter, utils_vlan_ports_list, duthosts, rand_one_dut_hostname):
    """
    Send tagged GARP packets from each port.
    Verify packets egress without tag from ports whose PVID same with ingress port.
    Verify packets egress with tag from ports who include VLAN ID but PVID different from ingress port.
    verify show arp command on DUT.
    """
    duthost = duthosts[rand_one_dut_hostname]
    router_mac = duthost.facts['router_mac']
    for vlan_port in utils_vlan_ports_list:
        port_index = vlan_port["port_index"][0]
        # Send GARP packets to switch to populate the arp table with dummy MACs for each port
        # Totally 10 dummy MACs for each port, send 1 packet for each dummy MAC
        # ARP table will be cleaned up before each iteration, so there won't be any conflict MAC and IP
        dummy_macs = ['{}:{:02x}:{:02x}'.format(DUMMY_MAC_PREFIX, port_index&0xFF, i+1)
                      for i in range(DUMMY_ARP_COUNT)]
        dummy_ips = ['{}.{:d}.{:d}'.format(DUMMY_IP_PREFIX, port_index&0xFF, i+1)
                      for i in range(DUMMY_ARP_COUNT)]
        for permit_vlanid in map(int, vlan_port["permit_vlanid"]):
            logger.info('Test ARP: interface %s, VLAN %u' % (vlan_port["dev"], permit_vlanid))
            # Perform ARP clean up
            arp_cleanup(duthost)
            for i in range(DUMMY_ARP_COUNT):
                pkt = build_arp_packet(permit_vlanid, dummy_macs[i], router_mac, dummy_ips[i])
                logger.info("Send tagged({}) packet from {} ...".format(permit_vlanid, port_index))
                testutils.send(ptfadapter, port_index, pkt)

            try:
                res = duthost.command('show arp')
                assert res['rc'] == 0
                logger.info('"show arp" output on DUT:\n{}'.format(pprint.pformat(res['stdout_lines'])))

                arp_cnt = 0
                for l in res['stdout_lines']:
                    # Address MacAddress Iface Vlan
                    items = l.split()
                    if len(items) != 4:
                        continue
                    # Vlan must be number
                    if not items[3].isdigit():
                        continue
                    arp_cnt += 1
                    ip = items[0]
                    mac = items[1]
                    ifname = items[2]
                    vlan_id = int(items[3])
                    assert ip in dummy_ips
                    assert mac in dummy_macs
                    # 'show arp' command gets iface from FDB table,
                    # if 'show arp' command was earlier than FDB table update, ifname would be '-'
                    if ifname == '-':
                        logger.info('Ignore unknown iface...')
                    else:
                        assert ifname == vlan_port["dev"]
                    assert vlan_id == permit_vlanid
                assert arp_cnt == DUMMY_ARP_COUNT
            except Exception as detail:
                logger.error("Except: {}".format(detail))

