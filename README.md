# aptv-logo-auto
APTV automatic logo matching and index updater

## Custom channel logos

Add persistent manual logo mappings to `custom-logos.json`. Use the exact channel ID under `byId`, or the EPG display name under `byName`. Verified custom entries override automatically matched logos and are preserved when the index is regenerated.

```json
{
  "byId": {
    "channel.example": "https://example.com/logos/channel.png"
  },
  "byName": {
    "Example Channel": "https://example.com/logos/channel.png"
  }
}
```

The repository is public. Use only public image URLs with no passwords, access tokens, or other credentials. Do not add M3U subscription URLs or stream URLs to this file. The generated `logos-map.json` is rebuilt by the workflow; edit `custom-logos.json` for persistent overrides. After editing, run **Actions → Update IPTV logo index → Run workflow**, or wait for the next scheduled run.
