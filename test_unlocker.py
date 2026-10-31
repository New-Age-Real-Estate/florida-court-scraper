import urllib.request
import json
import ssl

token = "a4588957-2e2d-49fb-b9f8-57edb82bc67c"
zone = "web_unlocker2"
target_url = "https://publicaccess.hillsclerk.com/oripublicaccess/"

url = "https://api.brightdata.com/request"
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {token}"
}

data = json.dumps({
    "zone": zone,
    "url": target_url,
    "format": "raw"
}).encode("utf-8")

req = urllib.request.Request(url, data=data, headers=headers)

print(f"[*] Sending request to Bright Data Web Unlocker for {target_url}...")
try:
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=30) as response:
        status_code = response.getcode()
        body = response.read().decode("utf-8", errors="replace")
        print(f"[+] Status Code: {status_code}")
        print(f"[+] Response length: {len(body)} characters")
        print(f"[+] First 300 characters of response:\n{body[:300]}")
        with open("hillsborough_sample.html", "w", encoding="utf-8") as f:
            f.write(body)
        print("[+] Saved response to hillsborough_sample.html")
except urllib.error.HTTPError as e:
    err_body = e.read().decode('utf-8', errors='replace')
    print(f"[-] HTTP Error {e.code}: {e.reason}")
    print(f"[-] Details: {err_body}")
except Exception as e:
    print(f"[-] Request failed: {e}")
