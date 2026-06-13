#!/usr/bin/env python3
"""Upload a cropped image into a Drive folder as the service account.
Usage: GOOGLE_SA_KEY=... python3 crop_upload.py <edits_folder_id> <local_image> [name]
Used by the cropping step of the CSE photo routine (propose-only output).
"""
import os, sys, json, mimetypes, urllib.request
from google.oauth2 import service_account
import google.auth.transport.requests as gtr

folder, path = sys.argv[1], sys.argv[2]
name = sys.argv[3] if len(sys.argv) > 3 else os.path.basename(path)
creds = service_account.Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_SA_KEY"]), scopes=["https://www.googleapis.com/auth/drive"])
creds.refresh(gtr.Request())

meta = json.dumps({"name": name, "parents": [folder]}).encode()
img = open(path, "rb").read()
ctype = mimetypes.guess_type(path)[0] or "image/jpeg"
boundary = "ROUTINEBOUNDARY7c9"
body = (b"--" + boundary.encode() + b"\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n" + meta +
        b"\r\n--" + boundary.encode() + f"\r\nContent-Type: {ctype}\r\n\r\n".encode() + img +
        b"\r\n--" + boundary.encode() + b"--")
req = urllib.request.Request(
    "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true",
    data=body, method="POST",
    headers={"Authorization": "Bearer " + creds.token,
             "Content-Type": f"multipart/related; boundary={boundary}"})
print(json.loads(urllib.request.urlopen(req).read()).get("id", "?"))
