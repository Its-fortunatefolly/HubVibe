# HubVibe human storefront preview
Isolated from the agent-side application. This is a visual/service-catalog preview, not a finished commerce site. No production payment, authentication, AI concierge, persistent inquiries, or backend execution is connected. Pricing requires validation and is not published yet.

From the repository root:
```sh
cd human-storefront
gcloud run deploy hubvibe-human --source . --region us-south1 --project YOUR_PROJECT --allow-unauthenticated
```
Local smoke test:
```sh
cd human-storefront
docker build -t hubvibe-human .
docker run -p 8080:8080 hubvibe-human
curl -f http://localhost:8080/health
```
Never deploy to the existing agent service. Before enabling real purchases, implement authentication, payment checkout and webhook verification, order state, durable inquiries, quotes, and actual AI/tool integrations.
