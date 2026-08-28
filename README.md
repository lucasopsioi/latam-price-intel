# LatAm Price Intelligence Hub

> **Educational use only.** This project is published for learning and demonstration.
> Commercial use is not permitted; anyone considering commercial use is solely
> responsible for legal and regulatory compliance in every applicable jurisdiction.
> See [LICENSE](LICENSE).

**A resident multi-agent pipeline that turns public retail listings across six countries into a daily competitive briefing.**

Scrape → clean → audit → match → brief, unattended, every day: 22 consumer-electronics brands, ~30 retail channels across Mexico, Brazil, Colombia, Chile, Peru and Argentina, delivered as comparison boards, launch trackers and a Telegram digest.

> Personal project built on personal time and equipment. It observes **publicly visible retail prices only** — no employer code, data or systems. Brand names in configs/tests are sanitized to fictional equivalents.

## Why it exists

Channel pricing in Latin America moves daily and differently in every country. Doing this by hand means opening dozens of retailer pages across six countries every morning — so nobody does it consistently, and pricing decisions get made on stale anecdotes. This pipeline made the morning sweep free.

## Architecture

1. **Scheduled scraping layer** — per-country browser workers with resilient selectors and retry budgets; runs as a Windows service, survives reboots.
2. **Multi-agent cleaning & audit** — LLM agents normalize messy listing titles into structured records (brand / model / variant / memory), a separate hygiene pass audits the extraction (`tests/test_llmhygiene.py` guards against prompt-injection from listing text and format drift), and a deterministic model-key matcher (`app/matching/`) reconciles the same product across channels and countries.
3. **Products on top** — price-comparison boards, a launch tracker (new SKUs appearing in any channel), and a daily Telegram briefing.

The interesting engineering is in the boring parts: audit-the-extractor tests, dictionary-guarded matching instead of trusting the LLM, and a config-first design (`config/runtime.example.yaml`) so channels/brands are data, not code.

## Run it

```bash
1-install.bat                      # deps + browser runtime + database
copy config\runtime.example.yaml config\runtime.yaml       # fill in your own tokens
copy config\my_products.example.csv config\my_products.csv # optional: own-product catalog (sample)
tools\install-service.ps1          # optional: run as a resident service
```

---
*Scraped observation data and browser profiles are not part of this repository.*
