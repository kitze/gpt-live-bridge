# Coolify Environment Setup (Action Required After Merge)

## Required environment variables

After merging PR #1, set these environment variables on Coolify for `gpt-live-bridge`:

### 🔐 Security (NEW - REQUIRED)

```bash
# Twilio signature validation (fail closed - required for production)
TWILIO_AUTH_TOKEN=<your_twilio_auth_token>

# Bridge shared secret (fail closed for /smoke/create-call and /outbound)
BRIDGE_SHARED_SECRET=<generate_random_secret>
```

**How to generate `BRIDGE_SHARED_SECRET`:**
```bash
# Generate a strong random secret
openssl rand -base64 32
```

### ✅ Already configured (keep these)

```bash
CODEX_LB_API_KEY=sk-clb-...
CODEX_LB_URL=https://codex-lb.service.beast.kitze.io
PUBLIC_BASE_URL=https://gpt-live-bridge.exposed.kitze.io
TWILIO_ACCOUNT_SID=<your_account_sid>
TWILIO_FROM_NUMBER=+48739670417
PORT=8080
```

## Twilio webhook configuration

Twilio Console → Phone Numbers → `+48 739670417`:

- **A call comes in** → Webhook **POST**:
  ```
  https://gpt-live-bridge.exposed.kitze.io/twilio/voice
  ```

## What changed

### Before (TEMP workaround)
- ❌ Twilio signature validation skipped by default (TEMP workaround)
- ❌ `/smoke/create-call` and `/outbound` publicly accessible
- ❌ Any random could burn gpt-live credits

### After (this PR)
- ✅ Proper Twilio signature validation with `RequestValidator`
- ✅ Tries multiple URL candidates to handle Worker/proxy rewrites
- ✅ Fails closed when `TWILIO_AUTH_TOKEN` is set
- ✅ `/smoke/create-call` and `/outbound` require `BRIDGE_SHARED_SECRET`
- ✅ No more TEMP skip in default code path

## Testing after deployment

1. **Health check:**
   ```bash
   curl -sS https://gpt-live-bridge.exposed.kitze.io/health | jq
   ```
   Should show:
   ```json
   {
     "ok": true,
     "auth_required": true,
     "twilio_signature_validation": true,
     ...
   }
   ```

2. **Twilio call test:**
   - Call `+48 739670417` from `+48 732125178`
   - Should connect successfully (signature validated)
   - Check Coolify logs for: `twilio start`, `bridge started`, `iceConnectionState=...`

3. **Bridge secret protection:**
   ```bash
   # Should fail (no secret)
   curl -sS https://gpt-live-bridge.exposed.kitze.io/smoke/create-call
   # Expected: HTTP 401 Forbidden

   # Should succeed (with secret)
   curl -sS -H "X-Bridge-Secret: YOUR_SECRET" \
     https://gpt-live-bridge.exposed.kitze.io/smoke/create-call
   # Expected: HTTP 200 with call details
   ```

## Emergency escape hatches (not recommended)

If signature validation causes issues after deployment:

```bash
# EMERGENCY ONLY: disable signature validation
TWILIO_SKIP_SIGNATURE=1
```

This should NOT be needed if `TWILIO_AUTH_TOKEN` and `PUBLIC_BASE_URL` are set correctly.

## Support

If you see HTTP 403 on Twilio calls after deployment:
1. Check Coolify logs for "Twilio signature mismatch" with URL candidates
2. Verify `TWILIO_AUTH_TOKEN` matches Twilio Console
3. Verify `PUBLIC_BASE_URL=https://gpt-live-bridge.exposed.kitze.io` (no trailing slash)
4. Report the logged URL candidates and we can debug further

PR: https://github.com/kitze/gpt-live-bridge/pull/1
