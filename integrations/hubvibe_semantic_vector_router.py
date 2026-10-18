"""Parallel, discovery-only HubVibe router. Never signs, settles, or buys.
Sparse TF-IDF vectors are lexical vectors, not neural embeddings.
"""
import argparse, json, math, re, urllib.request
from collections import Counter
from urllib.parse import urlparse

BASE = "https://hubvibe-io.com"
PHRASES = {
 "/audit/bundle": "full website audit WCAG accessibility SEO security performance all checks",
 "/audit/wcag": "accessibility WCAG axe violations",
 "/audit/seo": "search engine optimization metadata title canonical SEO",
 "/audit/security": "security headers HSTS CSP clickjacking",
 "/audit/performance": "website speed performance load time request size",
 "/work/market/quote": "live market crypto price quotes bitcoin BTC",
 "/work/market/prediction": "Polymarket prediction market odds event pricing",
 "/work/identity/check": "contact and sanctions risk screening not identity proof",
 "/work/email/verify": "email deliverability verification",
 "/work/company/enrich": "company enrichment organization profile",
}
def terms(s):
 return Counter(re.findall(r"[a-z0-9]+",s.lower()))

def load_catalog(base=BASE,timeout=5):
 p=urlparse(base)
 if p.scheme!="https" or p.username or p.password or not p.hostname: raise ValueError("HTTPS origin required")
 req=urllib.request.Request(base.rstrip("/")+"/openapi.json",headers={"Accept":"application/json"})
 with urllib.request.urlopen(req,timeout=timeout) as response:
  raw=response.read(8000001)
 if len(raw)>8000000: raise ValueError("catalog too large")
 doc=json.loads(raw)
 if not isinstance(doc,dict) or not isinstance(doc.get("paths"),dict): raise ValueError("invalid OpenAPI")
 return doc

def candidates(doc):
 rows=[]
 for path,ops in doc["paths"].items():
  if not isinstance(ops,dict) or not path.startswith(("/work/","/audit/")): continue
  spec=ops.get("post")
  if not isinstance(spec,dict): continue
  description=" ".join(str(spec.get(k,"")) for k in ("summary","description","operationId"))
  rows.append({"path":path,"method":"POST","description":description.strip(),"intent_phrases":PHRASES.get(path,"")})
 return rows

def search(intent,rows,limit=5):
 q=terms(intent)
 if not q: return []
 docs=[terms(" ".join([x["path"].replace("/"," "),x["description"],x["intent_phrases"]])) for x in rows]
 df=Counter(t for d in docs for t in d)
 n=len(docs)
 idf=lambda t: math.log((1+n)/(1+df[t]))+1
 qnorm=math.sqrt(sum((v*idf(t))**2 for t,v in q.items()))
 found=[]
 for row,d in zip(rows,docs):
  dot=sum(q[t]*d[t]*idf(t)**2 for t in q if t in d)
  norm=math.sqrt(sum((v*idf(t))**2 for t,v in d.items()))
  score=dot/(norm*qnorm) if norm and qnorm else 0
  if score>0: found.append({**row,"similarity":round(score,4),"pricing":"retrieve live purchase contract and 402"})
 return sorted(found,key=lambda x:-x["similarity"])[:max(0,limit)]

def main():
 parser=argparse.ArgumentParser(description="Local discovery only; no paid request or payment headers")
 parser.add_argument("intent",help="natural-language request")
 parser.add_argument("--catalog",help="local OpenAPI JSON for offline tests")
 parser.add_argument("--base",default=BASE,help="HTTPS catalog origin")
 args=parser.parse_args()
 doc=None
 if args.catalog:
  with open(args.catalog,encoding="utf-8") as f: doc=json.load(f)
 if doc is None: doc=load_catalog(args.base)
 print(json.dumps({"matches":search(args.intent,candidates(doc)),"billing_performed":False},indent=2))

if __name__=="__main__": main()
