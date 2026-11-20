import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECRET_RAW = b"test-signing-key-123456"
WH_SECRET = "whsec_" + base64.b64encode(SECRET_RAW).decode()


def sign(body: bytes, mid="msg_1", ts=None):
    ts = str(ts or int(time.time()))
    sig = base64.b64encode(hmac.new(SECRET_RAW, f"{mid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": mid, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
            "Content-Type": "application/json"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        os.environ["DATABASE_URL"] = "sqlite:///" + self.tmp.name
        os.environ["DODO_WEBHOOK_SECRET"] = WH_SECRET
        os.environ["DODO_PRODUCT_TOOLKIT"] = "pdt_toolkit"
        os.environ["DODO_PRODUCT_PRO"] = "pdt_pro"
        os.environ.pop("DODO_PAYMENTS_API_KEY", None)
        from app import create_app
        self.app = create_app({"TESTING": True})
        self.a = self.app.test_client()
        self.b = self.app.test_client()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def csrf(self, c, path="/login"):
        html = c.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" content="([^"]+)"', html).group(1)

    def signup(self, c, name, email):
        t = self.csrf(c, "/signup")
        return c.post("/signup", data={"csrf": t, "username": name, "email": email, "password": "password123"})

    def make_game(self, c, name="Game A"):
        t = self.csrf(c, "/dashboard")
        r = c.post("/game/new", data={"csrf": t, "name": name})
        return int(r.headers["Location"].rsplit("/", 1)[1])


class Tests(Base):
    def test_signup_login_logout(self):
        r = self.signup(self.a, "alice_1", "a@x.com")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.a.get("/dashboard").status_code, 200)
        t = self.csrf(self.a, "/dashboard")
        self.a.post("/logout", data={"csrf": t})
        self.assertEqual(self.a.get("/dashboard").status_code, 302)
        t = self.csrf(self.a)
        bad = self.a.post("/login", data={"csrf": t, "identifier": "a@x.com", "password": "nope"})
        self.assertEqual(bad.status_code, 401)
        ok = self.a.post("/login", data={"csrf": t, "identifier": "ALICE_1", "password": "password123"})
        self.assertEqual(ok.status_code, 302)

    def test_signup_rejects_duplicates_and_weak(self):
        self.signup(self.a, "alice_1", "a@x.com")
        r = self.signup(self.b, "Alice_1", "other@x.com")
        self.assertEqual(r.status_code, 400)
        t = self.csrf(self.b, "/signup")
        r = self.b.post("/signup", data={"csrf": t, "username": "bob", "email": "bad", "password": "short"})
        self.assertEqual(r.status_code, 400)

    def test_csrf_required(self):
        self.signup(self.a, "alice_1", "a@x.com")
        r = self.a.post("/game/new", data={"name": "x"})
        self.assertEqual(r.status_code, 400)

    def test_open_redirect_blocked(self):
        self.signup(self.a, "alice_1", "a@x.com")
        c = self.app.test_client()
        t = self.csrf(c)
        r = c.post("/login", data={"csrf": t, "identifier": "alice_1", "password": "password123",
                                   "next": "https://evil.example/"})
        self.assertTrue(r.headers["Location"].endswith("/dashboard"))

    def test_players_cannot_touch_each_others_data(self):
        self.signup(self.a, "alice_1", "a@x.com")
        self.signup(self.b, "bob_2", "b@x.com")
        gid = self.make_game(self.a)
        # add an item as alice
        page = self.a.get(f"/game/{gid}").get_data(as_text=True)
        cid = int(re.search(r'id="cat(\d+)"', page).group(1))
        t = self.csrf(self.a, f"/game/{gid}")
        self.a.post(f"/category/{cid}/items", data={"csrf": t, "text": "Secret item\nSecond"})
        page = self.a.get(f"/game/{gid}").get_data(as_text=True)
        iid = int(re.search(r'data-id="(\d+)"', page).group(1))
        self.assertIn("Secret item", page)
        # bob cannot see, toggle, delete, add to, or delete it
        tb = self.csrf(self.b, "/dashboard")
        self.assertEqual(self.b.get(f"/game/{gid}").status_code, 404)
        self.assertEqual(self.b.post(f"/item/{iid}/toggle", headers={"X-CSRF": tb}).status_code, 404)
        self.assertEqual(self.b.post(f"/item/{iid}/delete", data={"csrf": tb}).status_code, 404)
        self.assertEqual(self.b.post(f"/item/{iid}/note", data={"csrf": tb, "note": "x"}).status_code, 404)
        self.assertEqual(self.b.post(f"/category/{cid}/items", data={"csrf": tb, "text": "hack"}).status_code, 404)
        self.assertEqual(self.b.post(f"/game/{gid}/delete", data={"csrf": tb}).status_code, 404)
        self.assertNotIn("Secret item", self.b.get("/dashboard").get_data(as_text=True))
        # alice still has it, and can toggle it
        r = self.a.post(f"/item/{iid}/toggle", headers={"X-CSRF": self.csrf(self.a, "/dashboard")})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["done"])
        self.assertEqual(r.get_json()["game_total"], 2)

    def test_free_plan_limits_to_one_game(self):
        self.signup(self.a, "alice_1", "a@x.com")
        self.make_game(self.a)
        t = self.csrf(self.a, "/dashboard")
        r = self.a.post("/game/new", data={"csrf": t, "name": "Second"})
        self.assertIn("/store", r.headers["Location"])

    def test_toolkit_is_gated_and_webhook_unlocks_it(self):
        self.signup(self.a, "alice_1", "a@x.com")
        r = self.a.get("/toolkit")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/store", r.headers["Location"])
        uid = 1
        body = json.dumps({"type": "payment.succeeded", "data": {
            "payment_id": "pay_1", "metadata": {"user_id": str(uid), "product_key": "toolkit"}}}).encode()
        # bad signature rejected, nothing granted
        self.assertEqual(self.app.test_client().post("/webhooks/dodo", data=body,
                         headers={**sign(body), "webhook-signature": "v1,AAAA"}).status_code, 401)
        self.assertEqual(self.a.get("/toolkit").status_code, 302)
        # stale timestamp rejected
        self.assertEqual(self.app.test_client().post("/webhooks/dodo", data=body,
                         headers=sign(body, ts=int(time.time()) - 4000)).status_code, 401)
        # valid signature unlocks
        r = self.app.test_client().post("/webhooks/dodo", data=body, headers=sign(body))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.a.get("/toolkit").status_code, 200)
        # replay does not duplicate
        self.app.test_client().post("/webhooks/dodo", data=body, headers=sign(body))
        from app import Purchase
        with self.app.app_context():
            self.assertEqual(Purchase.query.count(), 1)

    def test_only_the_paying_user_is_unlocked(self):
        self.signup(self.a, "alice_1", "a@x.com")
        self.signup(self.b, "bob_2", "b@x.com")
        body = json.dumps({"type": "payment.succeeded", "data": {
            "payment_id": "pay_9", "metadata": {"user_id": "1", "product_key": "toolkit"}}}).encode()
        self.app.test_client().post("/webhooks/dodo", data=body, headers=sign(body))
        self.assertEqual(self.a.get("/toolkit").status_code, 200)
        self.assertEqual(self.b.get("/toolkit").status_code, 302)

    def test_product_matched_by_id_and_email_fallback_then_refund(self):
        self.signup(self.a, "alice_1", "a@x.com")
        body = json.dumps({"type": "payment.succeeded", "data": {
            "payment_id": "pay_2", "customer": {"email": "A@x.com"},
            "product_cart": [{"product_id": "pdt_pro", "quantity": 1}]}}).encode()
        self.app.test_client().post("/webhooks/dodo", data=body, headers=sign(body, mid="m2"))
        self.assertEqual(self.a.get("/api/entitlements").get_json()["owned"], ["pro"])
        rb = json.dumps({"type": "refund.succeeded", "data": {"payment_id": "pay_2"}}).encode()
        self.app.test_client().post("/webhooks/dodo", data=rb, headers=sign(rb, mid="m3"))
        self.assertEqual(self.a.get("/api/entitlements").get_json()["owned"], [])

    def test_checkout_without_keys_does_not_crash(self):
        self.signup(self.a, "alice_1", "a@x.com")
        t = self.csrf(self.a, "/store")
        r = self.a.post("/checkout/toolkit", data={"csrf": t})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/store", r.headers["Location"])
        t = self.csrf(self.a, "/store")
        self.assertEqual(self.a.post("/checkout/nonsense", data={"csrf": t}).status_code, 404)

    def test_pages_render(self):
        for p in ["/", "/signup", "/login", "/store", "/terms", "/privacy", "/refunds", "/healthz"]:
            self.assertEqual(self.a.get(p).status_code, 200, p)


if __name__ == "__main__":
    unittest.main()
