
from dnsServer import *


if __name__ == '__main__':
    dns_server = DnsRelayServer()
    dns_server.load_map()
    dns_server.startup()
    print('Finished')