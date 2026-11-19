"""Full Clear: a completion tracker for gamers.

Flask app with accounts, per-user data, paid unlocks through Dodo Payments.
Every database read for user data is scoped to the logged-in user, so players
can never see or change each other's games.
"""
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()
log = logging.getLogger("fullclear")

MAX_FREE_GAMES = 1
MAX_ITEMS_PER_GAME = 2000
MAX_LINES_PER_ADD = 200
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(24), unique=True, nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False)
    pw_hash = db.Column(db.String(255), nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow)
    games = db.relationship("Game", backref="user", cascade="all, delete-orphan",
                            order_by="Game.id")
    purchases = db.relationship("Purchase", backref="user", cascade="all, delete-orphan")


class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow)
    categories = db.relationship("Category", backref="game", cascade="all, delete-orphan",
                                 order_by="Category.id")


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey("game.id"), nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    items = db.relationship("Item", backref="category", cascade="all, delete-orphan",
                            order_by="Item.id")


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"), nullable=False, index=True)
    text = db.Column(db.String(200), nullable=False)
    note = db.Column(db.String(500), default="", nullable=False)
    done = db.Column(db.Boolean, default=False, nullable=False)
    done_at = db.Column(db.DateTime, nullable=True)


class Purchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    product_key = db.Column(db.String(40), nullable=False)
    payment_id = db.Column(db.String(120), unique=True, nullable=False)
    status = db.Column(db.String(20), default="paid", nullable=False)
    created = db.Column(db.DateTime, default=datetime.utcnow)


# -------------------------------------------------------------- products
def products():
    """Paid products. Product ids come from environment variables so you can
    paste them in on Render without touching code."""
    return {
        "pro": {
            "name": "Full Clear Pro",
            "price": os.environ.get("PRICE_PRO", "$4.99"),
            "tagline": "Unlimited games and your activity history.",
            "features": ["Track unlimited games", "14-day activity chart on your dashboard",
                         "Supporter badge on your profile"],
            "product_id": os.environ.get("DODO_PRODUCT_PRO", "").strip(),
        },
        "toolkit": {
            "name": "Creator Toolkit",
            "price": os.environ.get("PRICE_TOOLKIT", "$9.99"),
            "tagline": "Scripts, hooks and a 30-day calendar for guide creators.",
            "features": ["Video script templates", "Hook formulas for Shorts and TikTok",
                         "Thumbnail checklist", "30-day posting calendar"],
            "product_id": os.environ.get("DODO_PRODUCT_TOOLKIT", "").strip(),
        },
    }


def owned_keys(user):
    if not user:
        return set()
    return {p.product_key for p in user.purchases if p.status == "paid"}


# ------------------------------------------------------------------- app
def create_app(test_config=None):
    app = Flask(__name__)
    uri = os.environ.get("DATABASE_URL", "sqlite:///fullclear.db")
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    base = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)
        log.warning("SECRET_KEY is not set. Sessions will reset on every restart.")
    app.config.update(
        SECRET_KEY=secret,
        SQLALCHEMY_DATABASE_URI=uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=base.startswith("https://"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=256 * 1024,
        APP_BASE_URL=base,
    )
    if test_config:
        app.config.update(test_config)
    db.init_app(app)
    with app.app_context():
        db.create_all()

    fails = {}  # (ip, email) -> [timestamps] for login throttling

    # ------------------------------------------------------- request hooks
    @app.before_request
    def load_user_and_csrf():
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = db.session.get(User, uid)
            if g.user is None:
                session.pop("uid", None)
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.method == "POST" and request.endpoint != "dodo_webhook":
            sent = request.form.get("csrf") or request.headers.get("X-CSRF", "")
            if not hmac.compare_digest(sent, session["csrf"]):
                abort(400, "Your session expired. Go back, refresh the page and try again.")

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")
        if request.endpoint in ("login", "signup", "dashboard", "store", "game_page", "toolkit"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        return {"me": g.user, "owned": owned_keys(g.user), "csrf_token": session.get("csrf", ""),
                "catalog": products()}

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("message.html", title="Something went wrong",
                               body=getattr(e, "description", "Bad request.")), 400

    @app.errorhandler(404)
    def not_found(e):
        return render_template("message.html", title="Signal lost",
                               body="That page does not exist, or it is not yours."), 404

    # --------------------------------------------------------------- guards
    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return fn(*a, **kw)
        return wrapper

    def requires(key):
        def deco(fn):
            @wraps(fn)
            @login_required
            def wrapper(*a, **kw):
                if key not in owned_keys(g.user):
                    flash(f"{products()[key]['name']} is a paid unlock. Get it below.", "warn")
                    return redirect(url_for("store") + f"#{key}")
                return fn(*a, **kw)
            return wrapper
        return deco

    def safe_next(target):
        if not target:
            return None
        p = urlparse(target)
        if p.scheme or p.netloc or not target.startswith("/") or target.startswith("//"):
            return None
        return target

    # ----------------------------------------------------- ownership lookups
    def own_game(gid):
        return Game.query.filter_by(id=gid, user_id=g.user.id).first_or_404()

    def own_category(cid):
        return (Category.query.join(Game).filter(Category.id == cid, Game.user_id == g.user.id)
                .first_or_404())

    def own_item(iid):
        return (Item.query.join(Category).join(Game)
                .filter(Item.id == iid, Game.user_id == g.user.id).first_or_404())

    def game_stats(game):
        total = done = 0
        cats = []
        for c in game.categories:
            t = len(c.items)
            d = sum(1 for i in c.items if i.done)
            total += t
            done += d
            cats.append({"cat": c, "total": t, "done": d,
                         "pct": round(d / t * 100) if t else 0})
        return {"total": total, "done": done, "pct": round(done / total * 100) if total else 0,
                "cats": cats}

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if g.user:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            pw = request.form.get("password", "")
            errors = []
            if not USERNAME_RE.match(username):
                errors.append("Gamertag must be 3 to 24 letters, numbers or underscores.")
            if not EMAIL_RE.match(email) or len(email) > 254:
                errors.append("Enter a valid email address.")
            if len(pw) < 8:
                errors.append("Password must be at least 8 characters.")
            if len(pw) > 200:
                errors.append("Password is too long.")
            if not errors:
                if User.query.filter(db.func.lower(User.username) == username.lower()).first():
                    errors.append("That gamertag is taken.")
                if User.query.filter_by(email=email).first():
                    errors.append("An account with that email already exists. Log in instead.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("signup.html", username=username, email=email), 400
            user = User(username=username, email=email, pw_hash=generate_password_hash(pw))
            db.session.add(user)
            db.session.commit()
            session.clear()
            session["uid"] = user.id
            session["csrf"] = secrets.token_urlsafe(32)
            session.permanent = True
            flash(f"Welcome, {user.username}. Your account is ready.", "ok")
            return redirect(url_for("dashboard"))
        return render_template("signup.html", username="", email="")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("dashboard"))
        nxt = safe_next(request.args.get("next") or request.form.get("next"))
        if request.method == "POST":
            ident = request.form.get("identifier", "").strip().lower()
            pw = request.form.get("password", "")
            key = (request.remote_addr, ident)
            now = time.time()
            recent = [t for t in fails.get(key, []) if now - t < 900]
            fails[key] = recent
            if len(recent) >= 5:
                flash("Too many attempts. Wait 15 minutes and try again.", "error")
                return render_template("login.html", next=nxt, identifier=ident), 429
            user = User.query.filter(
                (User.email == ident) | (db.func.lower(User.username) == ident)).first()
            if user and check_password_hash(user.pw_hash, pw):
                fails.pop(key, None)
                session.clear()
                session["uid"] = user.id
                session["csrf"] = secrets.token_urlsafe(32)
                session.permanent = True
                return redirect(nxt or url_for("dashboard"))
            recent.append(now)
            fails[key] = recent
            flash("Wrong gamertag, email or password.", "error")
            return render_template("login.html", next=nxt, identifier=ident), 401
        return render_template("login.html", next=nxt, identifier="")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        games = [{"game": gm, **game_stats(gm)} for gm in g.user.games]
        total = sum(x["total"] for x in games)
        done = sum(x["done"] for x in games)
        activity = None
        if "pro" in owned_keys(g.user):
            since = datetime.utcnow().date() - timedelta(days=13)
            rows = (db.session.query(Item.done_at).join(Category).join(Game)
                    .filter(Game.user_id == g.user.id, Item.done == True,  # noqa: E712
                            Item.done_at >= datetime.combine(since, datetime.min.time())).all())
            counts = {}
            for (dt,) in rows:
                counts[dt.date()] = counts.get(dt.date(), 0) + 1
            activity = []
            for n in range(14):
                d = since + timedelta(days=n)
                activity.append({"label": d.strftime("%d %b"), "n": counts.get(d, 0)})
            peak = max([a["n"] for a in activity] + [1])
            for a in activity:
                a["h"] = round(a["n"] / peak * 100)
        can_add = len(g.user.games) < MAX_FREE_GAMES or "pro" in owned_keys(g.user)
        return render_template("dashboard.html", games=games, total=total, done=done,
                               pct=round(done / total * 100) if total else 0,
                               activity=activity, can_add=can_add)

    @app.post("/game/new")
    @login_required
    def game_new():
        name = request.form.get("name", "").strip()[:80]
        if not name:
            flash("Give the game a name.", "error")
            return redirect(url_for("dashboard"))
        if len(g.user.games) >= MAX_FREE_GAMES and "pro" not in owned_keys(g.user):
            flash("The free plan tracks one game. Pro unlocks unlimited games.", "warn")
            return redirect(url_for("store") + "#pro")
        gm = Game(user_id=g.user.id, name=name)
        gm.categories = [Category(name="Story missions"), Category(name="Collectibles"),
                         Category(name="Side activities")]
        db.session.add(gm)
        db.session.commit()
        return redirect(url_for("game_page", gid=gm.id))

    @app.get("/game/<int:gid>")
    @login_required
    def game_page(gid):
        gm = own_game(gid)
        return render_template("game.html", game=gm, stats=game_stats(gm))

    @app.post("/game/<int:gid>/delete")
    @login_required
    def game_delete(gid):
        db.session.delete(own_game(gid))
        db.session.commit()
        flash("Game deleted.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/game/<int:gid>/category")
    @login_required
    def category_new(gid):
        gm = own_game(gid)
        name = request.form.get("name", "").strip()[:60]
        if name:
            db.session.add(Category(game_id=gm.id, name=name))
            db.session.commit()
        return redirect(url_for("game_page", gid=gm.id))

    @app.post("/category/<int:cid>/delete")
    @login_required
    def category_delete(cid):
        c = own_category(cid)
        gid = c.game_id
        db.session.delete(c)
        db.session.commit()
        return redirect(url_for("game_page", gid=gid))

    @app.post("/category/<int:cid>/items")
    @login_required
    def items_add(cid):
        c = own_category(cid)
        existing = (db.session.query(db.func.count(Item.id)).join(Category)
                    .filter(Category.game_id == c.game_id).scalar())
        lines = [l.strip()[:200] for l in request.form.get("text", "").splitlines() if l.strip()]
        lines = lines[:MAX_LINES_PER_ADD]
        room = max(0, MAX_ITEMS_PER_GAME - existing)
        if len(lines) > room:
            flash(f"A game holds up to {MAX_ITEMS_PER_GAME} items. Some lines were skipped.", "warn")
            lines = lines[:room]
        for l in lines:
            db.session.add(Item(category_id=c.id, text=l))
        db.session.commit()
        return redirect(url_for("game_page", gid=c.game_id) + f"#cat{c.id}")

    @app.post("/item/<int:iid>/toggle")
    @login_required
    def item_toggle(iid):
        it = own_item(iid)
        it.done = not it.done
        it.done_at = datetime.utcnow() if it.done else None
        db.session.commit()
        gm = it.category.game
        s = game_stats(gm)
        cat = next(c for c in s["cats"] if c["cat"].id == it.category_id)
        return jsonify(done=it.done, cat_done=cat["done"], cat_total=cat["total"],
                       cat_pct=cat["pct"], game_done=s["done"], game_total=s["total"],
                       game_pct=s["pct"])

    @app.post("/item/<int:iid>/note")
    @login_required
    def item_note(iid):
        it = own_item(iid)
        it.note = (request.form.get("note") or (request.get_json(silent=True) or {}).get("note", ""))[:500]
        db.session.commit()
        return jsonify(ok=True)

    @app.post("/item/<int:iid>/delete")
    @login_required
    def item_delete(iid):
        it = own_item(iid)
        gid = it.category.game_id
        db.session.delete(it)
        db.session.commit()
        return redirect(url_for("game_page", gid=gid))

    # --------------------------------------------------- store and payments
    @app.get("/store")
    def store():
        return render_template("store.html", configured=bool(os.environ.get("DODO_PAYMENTS_API_KEY")))

    @app.post("/checkout/<key>")
    @login_required
    def checkout(key):
        prod = products().get(key)
        if not prod:
            abort(404)
        if key in owned_keys(g.user):
            flash("You already own this.", "ok")
            return redirect(url_for("store"))
        api_key = os.environ.get("DODO_PAYMENTS_API_KEY", "")
        if not prod["product_id"] or not api_key:
            flash("Payments are not switched on yet. Please check back soon.", "warn")
            return redirect(url_for("store"))
        host = ("https://live.dodopayments.com"
                if os.environ.get("DODO_ENV", "test") == "live" else "https://test.dodopayments.com")
        body = {
            "product_cart": [{"product_id": prod["product_id"], "quantity": 1}],
            "customer": {"email": g.user.email, "name": g.user.username},
            "metadata": {"user_id": str(g.user.id), "product_key": key},
            "return_url": f"{app.config['APP_BASE_URL']}/payment/return?product={key}",
            "cancel_url": f"{app.config['APP_BASE_URL']}/store",
        }
        try:
            r = requests.post(host + "/checkouts", json=body, timeout=15,
                              headers={"Authorization": f"Bearer {api_key}"})
            r.raise_for_status()
            url = r.json().get("checkout_url")
        except Exception as exc:  # network, 4xx, bad JSON
            log.error("Dodo checkout failed: %s", exc)
            url = None
        if not url or not url.startswith("https://"):
            flash("We could not start the checkout. Try again in a minute.", "error")
            return redirect(url_for("store"))
        return redirect(url)

    @app.get("/payment/return")
    @login_required
    def payment_return():
        key = request.args.get("product", "")
        # Access is granted only by the verified webhook, never by this URL.
        return render_template("payment_return.html", key=key, prod=products().get(key),
                               status=request.args.get("status", ""))

    @app.get("/api/entitlements")
    @login_required
    def entitlements():
        return jsonify(owned=sorted(owned_keys(g.user)))

    @app.post("/webhooks/dodo")
    def dodo_webhook():
        body = request.get_data()
        secret = os.environ.get("DODO_WEBHOOK_SECRET", "")
        if not verify_webhook(secret, request.headers, body):
            return "invalid signature", 401
        event = request.get_json(silent=True, force=True) or {}
        etype = event.get("type", "")
        data = event.get("data") or {}
        if etype == "payment.succeeded":
            grant_purchase(data)
        elif etype in ("refund.succeeded",):
            pid = data.get("payment_id")
            p = Purchase.query.filter_by(payment_id=pid).first() if pid else None
            if p:
                p.status = "refunded"
                db.session.commit()
        return "ok", 200

    def grant_purchase(data):
        meta = data.get("metadata") or {}
        payment_id = data.get("payment_id") or data.get("id")
        if not payment_id:
            return
        user = None
        if str(meta.get("user_id", "")).isdigit():
            user = db.session.get(User, int(meta["user_id"]))
        if user is None:
            email = ((data.get("customer") or {}).get("email") or "").lower()
            user = User.query.filter_by(email=email).first() if email else None
        key = meta.get("product_key")
        if key not in products():
            ids = {p["product_id"]: k for k, p in products().items() if p["product_id"]}
            for line in data.get("product_cart") or []:
                if line.get("product_id") in ids:
                    key = ids[line["product_id"]]
        if not user or key not in products():
            log.warning("Webhook payment %s could not be matched to a user/product", payment_id)
            return
        if Purchase.query.filter_by(payment_id=payment_id).first():
            return
        db.session.add(Purchase(user_id=user.id, product_key=key, payment_id=payment_id))
        db.session.commit()

    # -------------------------------------------------------- paid page
    @app.get("/toolkit")
    @requires("toolkit")
    def toolkit():
        return render_template("toolkit.html")

    # ------------------------------------------------------------ legal
    @app.get("/terms")
    def terms():
        return render_template("legal.html", page="terms")

    @app.get("/privacy")
    def privacy():
        return render_template("legal.html", page="privacy")

    @app.get("/refunds")
    def refunds():
        return render_template("legal.html", page="refunds")

    return app


def verify_webhook(secret, headers, body):
    """Standard Webhooks signature check used by Dodo Payments."""
    mid = headers.get("webhook-id")
    ts = headers.get("webhook-timestamp")
    sig = headers.get("webhook-signature")
    if not (secret and mid and ts and sig):
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    try:
        key = base64.b64decode(secret[6:]) if secret.startswith("whsec_") else secret.encode()
    except Exception:
        return False
    signed = f"{mid}.{ts}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in sig.split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


if __name__ == "__main__":
    create_app().run(debug=True)
