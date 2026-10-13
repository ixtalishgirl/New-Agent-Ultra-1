# WireGuard Germany Tunnel — setup instructions (template-side)

These steps assume ahsan procures his OWN VPS in Germany (his decision —
see `../../GERMANY.md`). Nothing here is provider-specific; it works on
any mainstream Linux VPS (Ubuntu/Debian shown).

## A. On the German VPS (server side)

1. Install WireGuard:
   ```
   sudo apt update && sudo apt install -y wireguard
   ```

2. Generate the server keypair:
   ```
   umask 077
   wg genkey | tee /etc/wireguard/server.key | wg pubkey > /etc/wireguard/server.pub
   cat /etc/wireguard/server.pub        # <-- copy into the client template
   ```

3. Create `/etc/wireguard/wg0.conf` on the VPS:
   ```
   [Interface]
   PrivateKey = <contents of /etc/wireguard/server.key>
   Address = 10.8.0.1/24
   ListenPort = 51820
   # NAT: masquerade tunnel traffic out the VPS's public interface
   PostUp   = iptables -t nat -A POSTROUTING -s 10.8.0.0/24 -o eth0 -j MASQUERADE
   PostDown = iptables -t nat -D POSTROUTING -s 10.8.0.0/24 -o eth0 -j MASQUERADE

   [Peer]
   # one [Peer] block per client machine
   PublicKey = <client.pub contents>
   AllowedIPs = 10.8.0.2/32
   ```
   (Replace `eth0` with the VPS's actual public interface name —
   check with `ip route | grep default`.)

4. Enable IP forwarding (persistently):
   ```
   echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf
   sudo sysctl -p
   ```

5. Open UDP 51820 in the VPS firewall/security group, then:
   ```
   sudo systemctl enable --now wg-quick@wg0
   ```

## B. On the browser machine (client side)

1. Install WireGuard:
   ```
   sudo apt update && sudo apt install -y wireguard
   ```

2. Generate the client keypair:
   ```
   umask 077
   wg genkey | tee client.key | wg pubkey > client.pub
   ```

3. Copy `wg-germany.conf.example` to `/etc/wireguard/wg-germany.conf`,
   fill in ALL placeholders (client private key, server public key,
   VPS endpoint, tunnel IPs, DNS), then lock permissions:
   ```
   sudo chmod 600 /etc/wireguard/wg-germany.conf
   ```

4. Bring the tunnel up:
   ```
   sudo wg-quick up wg-germany
   ```

5. Verify (from the repo root):
   ```
   python3 browser-system/germany/verify_german_ip.py
   ```
   Expect `appears German: yes` with a German city/ASN in the evidence.

6. To stop routing through Germany:
   ```
   sudo wg-quick down wg-germany
   ```

## C. Honest limitations (read before relying on this)

* A German IP ≠ German identity. Sites that check residency documents,
  local phone numbers, German payment methods, or account history will
  still ask — report the requirement honestly, never fabricate it.
* Some sites block datacenter IPs outright (VPS ranges are well known).
  If a site refuses the tunnel, that is a signal to report, not to
  "try harder" with shadier infra.
* Keep the VPS patched and the keys private. A compromised tunnel exit
  implicates the VPS owner (ahsan).
* Cost: a small German VPS is typically ~€4–6/month. There is no
  reliable free equivalent — see `../../GERMANY.md`.
