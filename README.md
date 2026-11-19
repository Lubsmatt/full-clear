# Full Clear

A completion tracker for gamers. Players sign up, keep their own private games and checklists, see progress rings and charts, and unlock paid extras through Dodo Payments.

## What is inside

| Page | Who can open it |
| --- | --- |
| `/` landing page | everyone |
| `/signup`, `/login` | everyone |
| `/dashboard` (games, progress ring, 14-day chart) | logged in. The chart needs **Pro** |
| `/game/<id>` (checklists, notes, search, filters) | the owner only |
| `/store` | everyone. Buying needs a login |
| `/toolkit` | **paid page**, needs the **Creator Toolkit** |
| `/terms`, `/privacy`, `/refunds` | everyone. Starter text, edit before launch |

Free accounts track 1 game. **Pro** unlocks unlimited games and the activity chart.

Every database query for player data is filtered by the logged-in user, so players cannot see or change each other's data. There are automated tests for this.

## Run it on your computer

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then edit; or export the variables yourself
python app.py            # http://localhost:5000
python -m unittest discover -s tests
```

Without Dodo keys the store shows "Payments are not switched on yet", which is expected.

## Deploy on Render

1. Push this folder to a GitHub repository.
2. In Render choose **New > Blueprint** and select the repo. It reads `render.yaml` and creates the web service and a Postgres database.
3. After the first deploy, open the service **Environment** tab and set:

| Variable | Value |
| --- | --- |
| `APP_BASE_URL` | your live URL, for example `https://full-clear.onrender.com` (no trailing slash) |
| `DODO_ENV` | `test` while testing, `live` when you go live |
| `DODO_PAYMENTS_API_KEY` | from Dodo dashboard, Developer > API Keys |
| `DODO_PRODUCT_PRO` | the product id of Full Clear Pro |
| `DODO_PRODUCT_TOOLKIT` | the product id of Creator Toolkit |
| `DODO_WEBHOOK_SECRET` | the signing secret of your webhook endpoint (starts with `whsec_`) |

`SECRET_KEY` and `DATABASE_URL` are filled in automatically.

## Connect Dodo Payments

1. In Dodo create two **one-time** products (Pro and Creator Toolkit) and copy each product id (looks like `pdt_...`).
2. Under Developer > Webhooks add the endpoint `https://YOUR-URL/webhooks/dodo` and subscribe to `payment.succeeded` and `refund.succeeded`. Copy the signing secret into `DODO_WEBHOOK_SECRET`.
3. Test in `test` mode with Dodo's test card, then switch `DODO_ENV=live` and use your live API key, product ids and webhook secret.

How a purchase works: the player clicks Buy, the app creates a Dodo checkout session with the player's id in its metadata, Dodo takes payment, then Dodo calls the webhook. The app checks the signature and only then unlocks the product. The return page never grants access by itself, so a player cannot unlock a page by editing a URL.

The webhook reads `data.metadata.user_id` and `data.metadata.product_key`. If those are missing it falls back to the customer email and to the product id in the cart. After your first test payment, confirm the unlock worked. If it did not, check the Render logs for "could not be matched".

## Add another paid page later

1. Add an entry to `products()` in `app.py` with a new key and an env variable for its product id.
2. Add a route decorated with `@requires("your_key")`.
3. Create the product in Dodo and set the new env variable on Render.

## Before you take real money

- Edit the legal pages and add your support email.
- Render free tiers have limits (sleeping services, database expiry). Check Render's current terms and upgrade the database before you rely on it.
- Do not use publisher names, logos or artwork in the brand. Mention games in plain text only.
