import pytest
import os
import json
from unittest.mock import patch, MagicMock

from app.modules.windscribe.service import WindscribeSyncService


def test_parse_wireguard_config_json():
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
            "allowedIPs": [
              "0.0.0.0/0",
              "::/0"
            ],
            "endpoint": "arn-476-wg.whiskergalaxy.com:443"
          }
        ],
        "noKernelTun": false
      },
      "tag": "WG-SW-GEMINI"
    }'''
    parsed = WindscribeSyncService.parse_wireguard_config(sample_json)
    assert parsed["secret_key"] == "cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4="
    assert len(parsed["address"]) == 2
    assert "100.79.238.251/32" in parsed["address"]
    assert "fd54:4::6f16:8109:bd6d:60d8/128" in parsed["address"]
    assert parsed["remote_dns"] == ["10.255.255.3"]
    assert parsed["preshared_key"] == "OJotyiYFyogAqjL7n9vlW2Vc/PhU4Dwes91TE7DHR0Y="
    assert parsed["public_key"] == "ycaVQ6uguxORaayOFZ3eyiqem/YTZUFSDVJZr6YptG4="
    assert parsed["endpoint"] == "arn-476-wg.whiskergalaxy.com:443"


def test_parse_wireguard_config_ini():
    sample_ini = """[Interface]
PrivateKey = cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4=
Address = 100.79.238.251/32, fd54:4::6f16:8109:bd6d:60d8/128
DNS = 10.255.255.3

[Peer]
PublicKey = ycaVQ6uguxORaayOFZ3eyiqem/YTZUFSDVJZr6YptG4=
PresharedKey = OJotyiYFyogAqjL7n9vlW2Vc/PhU4Dwes91TE7DHR0Y=
Endpoint = arn-476-wg.whiskergalaxy.com:443
AllowedIPs = 0.0.0.0/0, ::/0"""

    parsed = WindscribeSyncService.parse_wireguard_config(sample_ini)
    assert parsed["secret_key"] == "cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4="
    assert len(parsed["address"]) == 2
    assert parsed["address"][0] == "100.79.238.251/32"
    assert parsed["address"][1] == "fd54:4::6f16:8109:bd6d:60d8/128"
    assert parsed["remote_dns"] == ["10.255.255.3"]
    assert parsed["preshared_key"] == "OJotyiYFyogAqjL7n9vlW2Vc/PhU4Dwes91TE7DHR0Y="
    assert parsed["endpoint"] == "arn-476-wg.whiskergalaxy.com:443"


def test_save_and_get_wireguard_keys():
    res = WindscribeSyncService.save_wireguard_keys(
        secret_key="TEST_SECRET_KEY_12345678901234567890",
        address=["100.79.238.251/32", "fd54:4::6f16:8109:bd6d:60d8/128"],
        preshared_key="TEST_PRESHARED_KEY_1234567890",
        remote_dns=["10.255.255.3"],
        server_pubkey="TEST_SERVER_PUBKEY_1234567890"
    )
    assert res["success"] is True

    sk, addr, pub, psk, dns = WindscribeSyncService.get_wireguard_keys()
    assert sk == "TEST_SECRET_KEY_12345678901234567890"
    assert len(addr) == 2
    assert psk == "TEST_PRESHARED_KEY_1234567890"
    assert dns == ["10.255.255.3"]


def test_auto_detect_wireguard_keys_from_xray():
    mock_xray_cfg = {
        "outbounds": [
            {
                "tag": "direct",
                "protocol": "freedom"
            },
            {
                "tag": "WG-SW-GEMINI",
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
                    ],
                    "noKernelTun": False
                }
            }
        ]
    }
    with patch("app.modules.xui_backup.service.fetch_xray_config_via_api", return_value=(mock_xray_cfg, None)):
        det = WindscribeSyncService.auto_detect_wireguard_keys()
        assert det["success"] is True
        assert det["source_tag"] == "WG-SW-GEMINI"

        sk, addr, pub, psk, dns = WindscribeSyncService.get_wireguard_keys()
        assert sk == "cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4="
        assert len(addr) == 2
        assert dns == ["10.255.255.3"]
        assert psk == "OJotyiYFyogAqjL7n9vlW2Vc/PhU4Dwes91TE7DHR0Y="


def test_generated_outbounds_settings_standards():
    """Verify that generated outbounds adhere strictly to working specifications."""
    mock_xray_cfg = {
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
        "routing": {"balancers": []}
    }
    mock_nodes = {
        "DK": [
            {
                "balancer": "DK",
                "tag": "dk-copenhagen",
                "hostname": "cph-429-wg.whiskergalaxy.com",
                "ip": "185.232.25.30",
                "pubkey": "TEST_PUBKEY"
            }
        ]
    }

    with patch("app.modules.windscribe.service.WindscribeSyncService.fetch_candidate_nodes", return_value=mock_nodes), \
         patch("app.modules.xui_backup.service.fetch_xray_config_via_api", return_value=(mock_xray_cfg, MagicMock())), \
         patch("app.modules.xui_backup.service.create_xui_backup", return_value=None), \
         patch("app.modules.xui_backup.service.update_xray_config_via_api", return_value=(True, "OK")), \
         patch("app.modules.windscribe.service.WindscribeSyncService.restart_3x_container", return_value=True):

        res = WindscribeSyncService.sync_nodes()
        assert res["success"] is True

        outbounds = mock_xray_cfg["outbounds"]
        wd_outbounds = [o for o in outbounds if str(o.get("tag", "")).startswith("wd-")]
        assert len(wd_outbounds) == 1

        wd = wd_outbounds[0]
        assert wd["protocol"] == "wireguard"
        settings = wd["settings"]
        # CRITICAL CHECKS:
        assert settings["noKernelTun"] is False, "noKernelTun MUST be False!"
        assert settings["remoteDNS"] == ["10.255.255.3"], "remoteDNS must be ['10.255.255.3']!"
        assert settings["peers"][0]["allowedIPs"] == ["0.0.0.0/0", "::/0"], "allowedIPs must include ::/0!"
        assert settings["secretKey"] == "cEfL845rcOBM8tdD3c+J4Xaf5DpFD5M63vTBxQpGMk4="
