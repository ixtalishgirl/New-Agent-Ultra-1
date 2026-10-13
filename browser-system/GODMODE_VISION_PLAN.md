# GOD MODE VISION — Master Plan
**Date:** 2026-10-14 | **For:** ahsan | **By:** Haley Noor

## Tumne Kya Manga (Requirements)

1. **👀 Model ki Ankhein** — dekhe tum kya likh rahe ho (screen share)
2. **🌐 Website pe ankhein** — har page visually dekhe, CAPTCHA bhi
3. **🔒 Hamesha ON** — koi button/toggle se off na ho sake
4. **✅ Real** — fake nahi, asal screenshots
5. **⚡ Multi-task** — ek saath kai kaam
6. **🐛 Bug hunter** — foran batae bug hai ya nahi
7. **🏃 Long tasks** — bina ruke lambe kaam
8. **🇩🇪 Germany location** — German sites bina issue khulen

## Architecture (Soch Samajh Kar)

```
┌─────────────┐     screen share     ┌──────────────┐
│  Tumhara    │ ──── frames ────────▶│   Backend    │
│  Phone/PC   │                      │  (Render)    │
└─────────────┘                      └──────┬───────┘
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
              ┌─────▼─────┐         ┌──────▼──────┐        ┌──────▼──────┐
              │  VISION   │         │  BROWSER    │        │   MODEL     │
              │  CORE     │────────▶│  TOOLS      │───────▶│  (Qwen)     │
              │ (always   │  sees   │  (49 tools) │ calls  │  decides    │
              │   on!)    │         └─────────────┘        └─────────────┘
              └───────────┘
```

### Layer 1: Vision Core (ALWAYS ON) ✅ BUILT
- `vision_core.py` — hard-coded, no toggle, no off button
- Har tool call pe BEFORE + AFTER screenshot (automatic)
- `userscreen` — tumhari screen ke frames
- **Koi permission isko band nahi kar sakti**

### Layer 2: God-Mode Tools ✅ BUILT (19 new tools)
| System | Tools | Kaam |
|--------|-------|------|
| Vision Watch | 4 | 24/7 monitoring, instant see |
| Multi-task | 4 | Parallel browser tasks |
| Bug Hunter | 3 | Scan + triage + report |
| User Screen | 2 | Tumhari screen dekhe |
| Long Task | 6 | Checkpoint + resume |

### Layer 3: Germany Routing 🔧 NEEDS SETUP
- Proxy config code READY (`config.py`)
- Chahiye: German proxy/VPN credentials (env vars)
- `verify_german_ip.py` se test hoga
- **Tumhara faisla pending:** proxy khareedo ya defer?

### Layer 4: Frontend Screen Share 📱 TODO
- Screen share button (getDisplayMedia)
- Frames → backend WebSocket
- Tumhari typing agent dekhega

## Build Order (Plan ke Mutabiq)

- [x] Step 1: Vision Core (always-on, no toggle)
- [x] Step 2: 19 god-mode tools
- [x] Step 3: App.py me vision hook
- [ ] Step 4: Tests run karo
- [ ] Step 5: Frontend screen-share component
- [ ] Step 6: Germany proxy setup (tumhare approval se)
- [ ] Step 7: PERMISSIONS.md update (naye tools)
- [ ] Step 8: GitHub push + Render deploy

## Germany Ke Liye Chahiye
- German proxy/VPN service (tum khareedoge)
- Ya WireGuard config
- Env vars: `HTTPS_PROXY`, `BROWSER_PROXY_USERNAME`, `BROWSER_PROXY_PASSWORD`

## Honest Notes
- Vision REAL hai — asal Playwright screenshots, fake nahi
- CAPTCHA *dekh* sakta hai, *solve* karna alag permission hai
- 24/7 watch me interval 2s minimum (performance ke liye)
- Tumhari screen dekhne ke liye TUMHE share karna hoga (browser permission)
