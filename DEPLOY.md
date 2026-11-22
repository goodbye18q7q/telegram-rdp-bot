# 🚀 Live Deployment Details

Ye bot **Render free tier** par live hai.

| Cheez | Value |
|---|---|
| Telegram bot | **@Windows_hoster_bot** |
| Render service | `telegram-rdp-bot` (`srv-dc1m242fngtc738178v0`) |
| Render dashboard | https://dashboard.render.com/web/srv-dc1m242fngtc738178v0 |
| Service URL | https://telegram-rdp-bot.onrender.com |
| Health check | https://telegram-rdp-bot.onrender.com/health |
| Plan | **free** |
| GitHub repo | https://github.com/goodbye18q7q/telegram-rdp-bot |

## Keep-alive (free tier ke liye zaroori)

Render free web services **15 min idle par sleep** ho jate hain. Isliye
`.github/workflows/keepalive.yml` har **5 min** service ki `/health` URL ko ping karta hai —
service jaagti rehti hai, bot 24/7 polling karta rahta hai.

⚠️ GitHub scheduled workflows **60 din repo inactivity par disable** ho jate hain.
Repo me kuch bhi commit/push karo (ya workflow ko `workflow_dispatch` se manual run karo).

## Updates kaise karein

Render ka auto-deploy is repo par **active nahi** hai (GitHub App connected nahi hai),
isliye naye commit ke baad deploy manual trigger karna padta hai:

```bash
# Render API se manual deploy
curl -X POST \
  -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"clearCache":"do_not_clear"}' \
  "https://api.render.com/v1/services/srv-dc1m242fngtc738178v0/deploys"
```

Ya dashboard → service → **Manual Deploy**.

## Security

- `ALLOWED_USERS` env var me Telegram user ID(s) daalo — tabhi bot unhi ko control karega.
  (Khaali = sab allow ⚠️)
- RDP credentials `/connect` flow se chat me aate hain — wo message delete kar dena.
