import json
import re

def parse_wg(text):
    result = {
        'secret_key': '',
        'address': [],
        'remote_dns': ['10.255.255.3'],
        'preshared_key': '',
        'public_key': '',
        'endpoint': ''
    }
    cleaned = text.strip()
    if (cleaned.startswith('{') and cleaned.endswith('}')) or '"settings"' in cleaned:
        try:
            start = cleaned.find('{')
            end = cleaned.rfind('}') + 1
            data = json.loads(cleaned[start:end])
            settings = data.get('settings', data)
            result['secret_key'] = settings.get('secretKey', '')
            addr = settings.get('address', [])
            result['address'] = [a.strip() for a in addr.split(',')] if isinstance(addr, str) else [str(a).strip() for a in addr]
            dns = settings.get('remoteDNS', settings.get('dns', []))
            if dns:
                result['remote_dns'] = [d.strip() for d in dns.split(',')] if isinstance(dns, str) else [str(d).strip() for d in dns]
            peers = settings.get('peers', [])
            if peers:
                result['public_key'] = peers[0].get('publicKey', '')
                result['preshared_key'] = peers[0].get('preSharedKey', '')
                result['endpoint'] = peers[0].get('endpoint', '')
            if result['secret_key']:
                return result
        except Exception as e:
            pass

    priv_m = re.search(r'(?:PrivateKey|secretKey|private_key)\s*[:=]\s*["\']?([A-Za-z0-9+/=]{30,})["\']?', text, re.IGNORECASE)
    if priv_m:
        result['secret_key'] = priv_m.group(1).strip()
    
    addr_m = re.search(r'(?:Address|address)\s*[:=]\s*([^\r\n]+)', text, re.IGNORECASE)
    if addr_m:
        result['address'] = [a.strip().strip('"\'') for a in addr_m.group(1).strip().strip('"\'[]').split(',') if a.strip()]
        
    dns_m = re.search(r'(?:DNS|remoteDNS|dns)\s*[:=]\s*([^\r\n]+)', text, re.IGNORECASE)
    if dns_m:
        result['remote_dns'] = [d.strip().strip('"\'') for d in dns_m.group(1).strip().strip('"\'[]').split(',') if d.strip()]
        
    psk_m = re.search(r'(?:PresharedKey|preSharedKey|preshared_key)\s*[:=]\s*["\']?([A-Za-z0-9+/=]{30,})["\']?', text, re.IGNORECASE)
    if psk_m:
        result['preshared_key'] = psk_m.group(1).strip()
        
    pub_m = re.search(r'(?:PublicKey|publicKey|public_key)\s*[:=]\s*["\']?([A-Za-z0-9+/=]{30,})["\']?', text, re.IGNORECASE)
    if pub_m:
        result['public_key'] = pub_m.group(1).strip()
        
    ep_m = re.search(r'(?:Endpoint|endpoint)\s*[:=]\s*["\']?([A-Za-z0-9.\-_]+:\d+)["\']?', text, re.IGNORECASE)
    if ep_m:
        result['endpoint'] = ep_m.group(1).strip()
        
    return result

def test_json_parse():
    sample_json = '''{
      "protocol": "wireguard",
      "settings": {
        "mtu": 1420,
        "secretKey": "cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4=",
        "address": [
          "100.79.238.251/32",
          "fd54:4::6f16:8109:bd6d:60d8/128"
        ],
        "remoteDNS": [
          "10.255.255.3"
        ],
        "peers": [
          {
            "publicKey": "ycaVQ6uguxORaayOFZ3eyiqem/YTZUFSDVJZr6YptG4=",
            "preSharedKey": "OJotyiYFyogAqjL7n9vlW2Vc/PhU4Dwes91TE7DHR0Y=",
            "endpoint": "arn-476-wg.whiskergalaxy.com:443"
          }
        ]
      }
    }'''
    res = parse_wg(sample_json)
    assert res['secret_key'] == "cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4="
    assert len(res['address']) == 2
    assert res['remote_dns'] == ["10.255.255.3"]
    assert res['preshared_key'] == "OJotyiYFyogAqjL7n9vlW2Vc/PhU4Dwes91TE7DHR0Y="
    assert res['endpoint'] == "arn-476-wg.whiskergalaxy.com:443"

def test_ini_parse():
    sample_ini = """[Interface]
PrivateKey = cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4=
Address = 100.79.238.251/32, fd54:4::6f16:8109:bd6d:60d8/128
DNS = 10.255.255.3

[Peer]
PublicKey = ycaVQ6uguxORaayOFZ3eyiqem/YTZUFSDVJZr6YptG4=
PresharedKey = OJotyiYFyogAqjL7n9vlW2Vc/PhU4Dwes91TE7DHR0Y=
Endpoint = arn-476-wg.whiskergalaxy.com:443
AllowedIPs = 0.0.0.0/0, ::/0"""
    res = parse_wg(sample_ini)
    assert res['secret_key'] == "cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4="
    assert len(res['address']) == 2
    assert res['address'][0] == "100.79.238.251/32"
    assert res['address'][1] == "fd54:4::6f16:8109:bd6d:60d8/128"
    assert res['remote_dns'] == ["10.255.255.3"]
    assert res['preshared_key'] == "OJotyiYFyogAqjL7n9vlW2Vc/PhU4Dwes91TE7DHR0Y="
    assert res['endpoint'] == "arn-476-wg.whiskergalaxy.com:443"

if __name__ == "__main__":
    test_json_parse()
    test_ini_parse()
    print("ALL PARSER TESTS PASSED!")
