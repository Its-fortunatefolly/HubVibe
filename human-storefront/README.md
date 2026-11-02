# HubVibe human storefront preview
Isolated from the agent-side application. No production payment, auth, concierge AI, or backend execution is connected yet. No prices are fabricated: quote labels are placeholders pending final scoped pricing.
Build from repository root:
```sh
gcloud run deploy hubvibe-human --source human-storefront --region us-south1 --project YOUR_PROJECT --allow-unauthenticated
```
If building from source in that folder, use the included Dockerfile adjusted to COPY local folder instead of human-storefront/. Alternatively:
```sh
docker build -f human-storefront/Dockerfile -t hubvibe-human .
docker run -p 8080:8080 hubvibe-human
```
Do not deploy to the existing agent service. Before live checkout, implement auth, payment webhooks, order state, quote approval, durable inquiries, and real concierge integration.
