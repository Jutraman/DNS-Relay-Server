import socketserver
import struct
import socket
import threading
import sys
import time
from fileIO import *


file_name = 'dnsrelay.txt'
outer = ('10.3.9.4', 53)
#outer = ('192.168.43.1', 53)
BUFSIZE = 1024
global domainmap
id_map = {}
#ip_map saves pairs of id and the ip where it comes from
#ip_map saves pairs of id that relay-server sets and id from client, like(1, 2331)
task_queue = []
#task_queue saves the pairs of socket, the data waiting to be relayed and client's ip


class DnsQuery:
    #from question part, get domain address which need to be queried
    def __init__(self, data):
        i = 1
        self.domain = ''
        self.ip = ''
        while True:
            d = data[i]
            if d == 0:
                #ASCII = 0, then end up the deal
                break
            elif d < 32:
                #Add '.' between domain address
                self.domain += '.'
            else:
                self.domain += chr(d)
            i += 1
        self.package = data[0: i + 1]
        (self.type, self.classify) = struct.unpack('!HH', data[i + 1: i + 5])
        self.len = i + 5

    def get_bytes(self):
        return self.package + struct.pack('!HH', self.type, self.classify)    #!--big endian  H--unsigned short


class DnsAnswer:
    #write the answer part in dns package if needs
    def __init__(self, ip):
        self.name = 49164                       #c00c   当报文中域名重复出现时，就需要使用2字节的偏移指针来替换，一般响应报文中，资源部分的域名都是指针C00C(1100000000001100)，刚好指向请求部分的域名
        self.type = 1							#1	A	IPv4地址。
        self.classify = 1
        self.ttl = 190
        self.datalength = 4						#表示资源数据的长度（以字节为单位，如果资源数据为IP则为0004）
        self.ip = ip

    def get_bytes(self):
        pack = struct.pack('!HHHLH', self.name, self.type, self.classify, self.ttl, self.datalength)
        iplist = self.ip.split('.')
        pack = pack + struct.pack('BBBB', int(iplist[0]), int(iplist[1]), int(iplist[2]), int(iplist[3]))
        return pack


class DnsAnalyzer:
    #DNS analyzer is used to unpack and analyse data in DNS requests
    #As be a frame, it need initialized by DnsQuery
    def __init__(self, data):
        (self.Id, self.Flags, self.QdCount, self.AnCount, self.NsCount, self.ArCount) = \
            struct.unpack('!6H', data[0: 12])
        self.query = DnsQuery(data[12:])       #将请求数据原样返回

    def get_id(self):
        return self.Id

    def set_id(self, i):
        self.Id = i

    def set_rcode(self, rcode):
        self.Flags = self.Flags//16 * 16 + rcode

    def get_qr(self):                            #0表示查询报文，1表示响应报文,右移15位得到第一位
        qr = (self.Flags >> 15) % 2
        #print('> QR is : %d' % qr)
        return qr

    def get_domain(self):
        #get the domain in Question part of DNS package
        return self.query.domain

    def set_ip(self, ip):
        #set ip of reply package
        self.Answer = DnsAnswer(ip)
        self.AnCount = 1
        self.Flags = 33152

    def get_ip(self, reply):                            		#rdata存放的是ip地址,ip必须经过转换客户端才能识别
        #get IP from Answer part when the it is reply package
        #rdata是4字节，ip地址从.处切开后是由4段数字组成，每段数据不会超过2^8 === 256—一个字节(8bit),那rdata的4个字节刚好可以存放下一个ip地址。 
        ip = ''
        i = self.query.len + 12
        #according structure of Answer, RDATA starts from the 13th byte of Answer
        if_got = False
        while i < len(reply) and not if_got:
            if reply[i] == 0xc0 and i+3 < len(reply) and reply[i+3] == 0x01:
                if_got = True
                i += 12
            else:
                i += 1
        ip += str(reply[i])
        ip += '.'
        ip += str(reply[i+1])
        ip += '.'
        ip += str(reply[i+2])
        ip += '.'
        ip += str(reply[i+3])
        #print('%d.%d.%d.%d' % (reply[i], reply[i+1], reply[i+2], reply[i+3]))
        return ip

    def response(self):
        pack = struct.pack('!6H', self.Id, self.Flags, self.QdCount, self.AnCount, self.NsCount, self.ArCount)
        pack = pack + self.query.get_bytes()
        if self.AnCount != 0:
            pack += self.Answer.get_bytes()
        return pack

    def request(self, i):
        tmp = 0xff
        tmp = i & tmp
        self.set_id(tmp)
        pack = struct.pack('!6H', self.Id, self.Flags, self.QdCount, self.AnCount, self.NsCount, self.ArCount)
        pack = pack + self.query.get_bytes()
        return pack


class DnsUdpHandler(socketserver.BaseRequestHandler):     #BaseRequestHandler是所有请求处理对象的超类。它定义了接口，一个具体的请求处理程序子类必须重写基类的handle()方法
    #request handle class
    #UdpHandler is used to handle DNS query
    def handle(self):
        data = self.request[0].strip()          #self.request consists of a pair of data and client socket
        sock = self.request[1]
        analyzer = DnsAnalyzer(data)
        dnsmap = domainmap
        #print(dnsmap)

        if analyzer.query.type == 1:
            #print(data)
            #query wants the ip of domain
            domain = analyzer.get_domain()
            if dnsmap.__contains__(domain):
                #domain is found on local server
                reply_ip = dnsmap[domain]
                analyzer.set_ip(reply_ip)
                if reply_ip == "0.0.0.0":
                    analyzer.set_rcode(3)
                print('- Domain exists on local server..')
                print('> Domain:  ' + domain)
                print('> Ip    :  ' + reply_ip + '\n')
                sock.sendto(analyzer.response(), self.client_address)        #self.client_adress是客户端的地址
                #print('- Package: %s\n' % analyzer.response())
            else:
                #add the task to task_queue, waiting to be relayed
                print('- Domain doesn\'t exist on local server. Request it from outer server.')
                task_queue.append((sock, data, self.client_address))
        else:
            sock.sendto(data, self.client_address)


class DnsRelayServer:
    #dns relay server

    def __init__(self, port=53):
        self.port = port
        self.relay_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)     #数据报套接字类型为SOCK_DGRAM

    @staticmethod
    def load_map():
        global domainmap
        domainmap = load_table(file_name)
        #variable map is a dictionary whose key is domain address and value is ip.
        if domainmap is not None:
            print('--OK. Table has been loaded.')

    def startup(self):
        #start up the relay thread and server thread
        host, port = '127.0.0.1', self.port
        print('> Server startup...\n> Bind UDP socket -- address, port: %s : %s\n' % (host, port))
        threading.Thread(target=self.relay_thread).start()
        server = socketserver.UDPServer((host, port), DnsUdpHandler)
        server.serve_forever()

    def relay_thread(self):
        #start a loop to deal with task queue
        index = 0
        while True:
            if len(task_queue) > 0:
                #when there exists tasks
                if index < 1024:
                    index += 1
                else:
                    index = 0

                sock, data, client_address = task_queue[0]
                analyzer = DnsAnalyzer(data)
                id_map[index] = analyzer.get_id()
                self.relay_sock.sendto(analyzer.request(index), outer)

                self.relay_sock.setblocking(0)
                time.sleep(2)
                #设置为非阻塞状态,并等待2秒用于接收
                reply, addr = self.relay_sock.recvfrom(BUFSIZE)
                if len(reply) == 0:
                    print('- Fail to receive the reply from outer server.\n')
                else:
                    #print('- Address: %s\n- Package: %s\n' % (addr, reply))
                    reply_analyzer = DnsAnalyzer(reply)
                    domain = reply_analyzer.get_domain()
                    reply_ip = reply_analyzer.get_ip(reply)
                    print('- Get reply from outer server..')
                    print('> Domain:  ' + domain)
                    print('> Ip    :  ' + reply_ip + "\n")
                    domainmap[domain] = reply_ip
                    save_table(file_name, domain, reply_ip)

                    rest = reply[2:]
                    Id = id_map[index]
                    reply = struct.pack('!H', Id) + rest
                    sock.sendto(reply, client_address)
                    #print(reply)
                task_queue.pop(0)