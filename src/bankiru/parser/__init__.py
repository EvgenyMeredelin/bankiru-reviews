# ── Parser sub-package ────────────────────────────────────────────────────────
#
# This package implements the banki.ru web scraper (the "parser" service in
# docker-compose.yml). It runs as a long-lived APScheduler cron job that
# crawls banki.ru once daily, collects negative customer reviews (ratings 1–2),
# and POSTs them to the API service for storage.
#
# Key modules:
#   __main__.py  — entrypoint: configures Logfire, starts APScheduler, handles
#                  SIGHUP for live schedule reload
#   runner.py    — orchestrates a single crawl run: creates the client and
#                  crawler, collects reviews, POSTs them to the API
#   crawler.py   — sequential page-by-page crawler: iterates products, listing
#                  pages, and detail pages within a date window
#   client.py    — async HTTP client with randomised pacing and unlimited retry
#                  on connect errors (ban-avoidance strategy)
#   settings.py  — static constants: product catalog, URL templates, regex
#                  patterns, User-Agent pool, and base HTTP headers
#   tools.py     — text cleaning pipeline: HTML tag removal, emoji stripping,
#                  whitespace normalisation
#
# Data flow:
#   APScheduler (cron trigger)
#     → runner.run_once()
#       → BankiruClient (HTTP with pacing)
#         → BankiruCrawler (page iteration + extraction)
#           → clean_text_pipe (text normalisation)
#       → POST /reviews on the API service
