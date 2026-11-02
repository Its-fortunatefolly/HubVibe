import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path in ("/", "/index.html"):
   data=Path(__file__).with_name("index.html").read_bytes()
   self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
  elif self.path=="/health":
   data=b'{"status":"ok"}';self.send_response(200);self.send_header("Content-Type","application/json");self.end_headers();self.wfile.write(data)
  else:self.send_error(404)
 def do_POST(self):
  if self.path!="/api/inquiry":return self.send_error(404)
  try:
   n=int(self.headers.get("Content-Length","0"))
   if n<1 or n>4096:return self.send_error(413)
   payload=json.loads(self.rfile.read(n));msg=payload.get("message","")
   if not isinstance(msg,str) or len(msg)>2000:return self.send_error(400)
   response={"message":"I can help you scope that. This preview is not connected to live quoting, payments, or a human support team yet, so no request has been submitted."}
   data=json.dumps(response).encode()
   self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
  except (ValueError,UnicodeDecodeError):self.send_error(400)
ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","8080"))),Handler).serve_forever()
