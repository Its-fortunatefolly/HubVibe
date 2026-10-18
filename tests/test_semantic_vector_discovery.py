"""Offline tests for discovery: no network and no paid calls."""
import unittest
from integrations.hubvibe_semantic_vector_router import candidates, search, load_catalog

DOC={"paths":{
 "/audit/bundle":{"post":{"summary":"Full website audit"}},
 "/work/market/quote":{"post":{"summary":"Live market quote"}},
 "/work/identity/check":{"post":{"summary":"Risk screening"}},
 "/not_real":{"post":{"summary":"Fake endpoint"}},
 "/work/fake":{"get":{"summary":"Not POST"}}
}}
class DiscoveryTests(unittest.TestCase):
 def test_only_real_post_paths(self):
  self.assertEqual(set(x["path"] for x in candidates(DOC)),{"/audit/bundle","/work/market/quote","/work/identity/check"})
 def test_bundle_intent(self):
  self.assertEqual(search("website accessibility SEO performance audit",candidates(DOC))[0]["path"],"/audit/bundle")
 def test_market_intent(self):
  self.assertEqual(search("bitcoin BTC live market quote",candidates(DOC))[0]["path"],"/work/market/quote")
 def test_no_invented_route(self):
  self.assertEqual(search("zqzzxvv",candidates(DOC)),[])
 def test_no_price_or_payment(self):
  results=search("company risk sanctions identity",candidates(DOC))
  self.assertEqual(results[0]["path"],"/work/identity/check")
  self.assertNotIn("price_usd",results[0])
 def test_empty_catalog(self):
  self.assertEqual(search("market",candidates({"paths":{}})),[])
if __name__=="__main__": unittest.main()
