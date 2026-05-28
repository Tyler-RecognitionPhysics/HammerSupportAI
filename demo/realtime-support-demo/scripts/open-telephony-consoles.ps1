# Opens Twilio + OpenAI console pages for phone setup.
Start-Process "https://console.twilio.com/us1/develop/phone-numbers/manage/incoming"
Start-Process "https://console.twilio.com/us1/develop/phone-numbers/search"
Start-Process "https://console.twilio.com/us1/develop/voice/sip-trunks"
Start-Process "https://platform.openai.com/settings"
Write-Host "Opened: Twilio buy number, SIP trunks, OpenAI settings (Webhooks + General for project ID)."
