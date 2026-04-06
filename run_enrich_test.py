"""Quick test: enrich the 18 Miami solar companies."""
from __future__ import annotations
import json, time
from main import _enrich_single

with open('data/solar_miami_raw.json') as f:
    businesses = json.load(f)

print(f'Enriching {len(businesses)} businesses...')
enriched = []
for i, biz in enumerate(businesses, 1):
    biz['niche'] = 'solar'
    result = _enrich_single(biz)
    enriched.append(result)
    dm = result.get('decision_maker_name', '')
    email = result.get('email', '')
    method = result.get('email_method', '')
    s = 'Y' if email else 'N'
    print(f'{i:2}. {s} {result["name"][:35]:35} | DM: {dm[:20]:20} | {email[:35]} ({method})')
    time.sleep(0.5)

with_dm = sum(1 for b in enriched if b.get('decision_maker_name'))
with_email = sum(1 for b in enriched if b.get('email'))
personal = sum(1 for b in enriched if b.get('email_type') in ('decision_maker', 'personal_pattern'))
print(f'\nTotal: {len(enriched)} | DM: {with_dm} | Email: {with_email} | Personal: {personal}')

with open('data/solar_miami_enriched.json', 'w') as f:
    json.dump(enriched, f, indent=2, default=str)
print('Saved to data/solar_miami_enriched.json')
