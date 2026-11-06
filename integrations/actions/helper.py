import ipaddress
import json
import socket
import struct
import subprocess
from pathlib import Path


def execute(request, policy):
    if not isinstance(request,dict) or set(request) != {'action','ip'} or request['action'] not in ('ban','unban'):
        return {'ok':False,'detail':'Invalid action'}
    try:
        address = ipaddress.ip_address(request['ip'])
    except (ValueError,TypeError):
        return {'ok':False,'detail':'Invalid IP'}
    if request['action']=='ban' and (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified):
        return {'ok':False,'detail':'Protected address'}
    if request['action']=='ban' and any(address in ipaddress.ip_network(network,strict=False) for network in policy['trusted_ips']):
        return {'ok':False,'detail':'Protected address'}
    command = ['/usr/bin/fail2ban-client','set','sshd','banip' if request['action']=='ban' else 'unbanip',str(address)]
    try:
        process = subprocess.run(command,capture_output=True,timeout=7)
    except (OSError,subprocess.TimeoutExpired):
        return {'ok':False,'detail':'Fail2ban unavailable'}
    return {'ok':process.returncode == 0,'detail':('IP blocked' if request['action']=='ban' else 'IP unblocked') if process.returncode == 0 else 'Fail2ban rejected action'}


def main():
    import pwd
    connection = socket.socket(fileno=0)
    connection.settimeout(10)
    try:
        _,uid,_ = struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid != pwd.getpwnam('gatewatch').pw_uid:
            return
        data = bytearray()
        while b'\n' not in data:
            chunk = connection.recv(1024)
            if not chunk:
                return
            data.extend(chunk)
            if len(data)>2048:
                return
        request = json.loads(data.split(b'\n',1)[0])
        policy = json.loads(Path('/etc/gatewatch/actions-policy.json').read_text())
        result = execute(request,policy)
        connection.sendall(json.dumps(result).encode()+b'\n')
    except (OSError,ValueError,KeyError,TypeError):
        try:
            connection.sendall(b'{"ok":false,"detail":"Invalid action request"}\n')
        except OSError:
            pass
    finally:
        connection.close()


if __name__ == '__main__':
    main()
