# Proxy Setup Guide for Geoblocked Regions

## The Problem
You're seeing "SORRY... You cannot buy this product from the country/region you live in" when accessing DLsite. Even the DLsite API returns `not_found` for products that exist in Japan because **DLsite serves different product catalogs based on your IP address**.

## The Solution
Route DLsite requests through a **Japanese proxy or VPN** so the app appears to be accessing DLsite from Japan.

---

## Option 1: Free Proxy (Quick Test)

### Step 1: Find a Free Japanese Proxy
Visit: https://www.proxy-list.download/HTTPS (or similar)
- Filter by **Country: Japan**
- Copy a proxy URL like: `http://123.456.789.0:8080`

### Step 2: Configure the App
Add to your `.env` file (create if it doesn't exist):
```
DRAMACD_DLSITE_PROXY=http://123.456.789.0:8080
```

### Step 3: Restart Server
```bash
# Stop server (Ctrl+C)
# Start again
python main.py
```

### Step 4: Check Logs
You should see:
```
[INFO] Using proxy for DLsite requests: http://123.456.789.0:8080
```

### Step 5: Test Metadata Refresh
1. Open an item with blank metadata
2. Click "Refresh Metadata" button
3. Should now succeed!

**Note**: Free proxies are **slow and unreliable**. Use for testing only.

---

## Option 2: SOCKS5 Proxy (Recommended)

If you have a **local SOCKS5 proxy** (e.g., SSH tunnel, Shadowsocks, V2Ray):

### Example: SSH Tunnel to Japanese Server
```bash
# Create SOCKS5 proxy on localhost:1080
ssh -D 1080 -N user@japanese-server.com
```

### Configure App
Add to `.env`:
```
DRAMACD_DLSITE_PROXY=socks5://127.0.0.1:1080
```

### Restart and Test
Same as Option 1.

---

## Option 3: System-Wide VPN (Easiest)

### Step 1: Connect to Japanese VPN
Use any VPN service (ProtonVPN, NordVPN, etc.) and connect to a **Japan server**.

### Step 2: No Configuration Needed
Since the VPN routes ALL traffic through Japan, the app will automatically work.

### Step 3: Verify IP Address
Check your current IP:
```bash
curl https://ipinfo.io/country
# Should return: JP
```

### Step 4: Test Metadata Refresh
Same as Option 1 - should now work without proxy configuration.

---

## Option 4: Cloudflare WARP (Free, Simple)

Cloudflare WARP is a **free VPN** that can route traffic through different regions.

### Step 1: Install Cloudflare WARP
Download: https://1.1.1.1/
- Windows/macOS/Linux supported
- Free tier available

### Step 2: Connect
1. Open Cloudflare WARP app
2. Click "Connect"
3. Your IP will be routed through Cloudflare's network

### Step 3: Test
Check if you can access DLsite without geoblocking:
```bash
curl https://www.dlsite.com/maniax/api/=/product.json?workno=RJ01286723
```

Should return JSON instead of "SORRY".

---

## Troubleshooting

### Error: `ProxyError` or `ConnectTimeout`
**Cause**: Proxy is down or unreachable
**Fix**: Try a different proxy server

### Error: Still seeing `not_found`
**Cause**: Product code genuinely doesn't exist (even in Japan)
**Fix**: Double-check the product code on DLsite.com/maniax

### Error: `SSL certificate verify failed`
**Cause**: Some proxies break SSL
**Fix**: Add to `.env`:
```
DRAMACD_DLSITE_PROXY=http://proxy:port
# Or use HTTPS proxy instead of HTTP
```

### Proxy is Slow
**Cause**: Free proxies have limited bandwidth
**Fix**:
- Use a paid VPN service
- Use local SOCKS5 tunnel (SSH)
- Use Cloudflare WARP

---

## Verifying Proxy Works

### Test 1: Check IP Address
```bash
curl https://ipinfo.io/country
# Should return: JP (if using VPN)
# Or: Your original country (if using app-level proxy)
```

### Test 2: Direct API Test
```bash
curl "https://www.dlsite.com/maniax/api/=/product.json?workno=RJ01286723"
# Should return JSON, not "SORRY"
```

### Test 3: App Metadata Refresh
1. Override product code to `RJ01286723`
2. Click "Refresh Metadata"
3. Should see green success message with Japanese title

---

## Proxy URL Formats

| Type | Format | Example |
|------|--------|---------|
| HTTP | `http://host:port` | `http://proxy.example.com:8080` |
| HTTPS | `https://host:port` | `https://proxy.example.com:8443` |
| SOCKS5 | `socks5://host:port` | `socks5://127.0.0.1:1080` |
| With Auth | `http://user:pass@host:port` | `http://username:password@proxy.example.com:8080` |

---

## Recommended Solution

**For long-term use**:
1. **Cloudflare WARP** (free, easy, no configuration)
2. **Paid VPN with Japan server** (reliable, fast)

**For testing**:
1. Free proxy from proxy-list.download (quick, unreliable)

**For power users**:
1. SSH tunnel to Japanese VPS (full control, fast)

---

## Performance Impact

| Method | Speed | Reliability | Cost |
|--------|-------|-------------|------|
| Free Proxy | ⚠️ Slow (1-5 MB/s) | ⚠️ Low (50% uptime) | ✅ Free |
| Cloudflare WARP | ✅ Fast (10-50 MB/s) | ✅ High (99% uptime) | ✅ Free |
| Paid VPN | ✅ Fast (20-100 MB/s) | ✅ High (99% uptime) | 💰 $5-10/month |
| SSH Tunnel | ✅ Very Fast (depends on VPS) | ✅ Very High | 💰 $5/month (VPS) |

---

## Security Notes

⚠️ **Free proxies** can:
- Log your traffic
- Inject ads/malware
- Steal credentials

✅ **Safe options**:
- Cloudflare WARP (trusted company)
- Paid VPN services (reputable providers)
- Self-hosted SSH tunnel (full control)

---

## Next Steps

1. **Choose a method** (I recommend Cloudflare WARP for free + easy)
2. **Configure proxy** (or connect VPN)
3. **Restart server** (if using proxy config)
4. **Test with "Refresh Metadata"** button
5. **Enjoy full DLsite access!** 🎉

---

## Alternative: Manual Metadata Entry

If you **cannot use a proxy or VPN**, you can:
1. Manually download cover images from DLsite (while connected to VPN on your browser)
2. Use the "Show Cover" → Upload feature
3. Manually enter metadata fields (not automated, but works)

This is tedious but avoids needing a proxy for the app.
