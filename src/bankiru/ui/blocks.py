"""Gradio UI for browsing/filtering/summarizing the bankiru reviews dataset.

This module defines the Gradio Blocks layout and event handlers that make up
the user-facing review query interface. It is mounted at /gradio by app.py.

The UI provides:
  - Date range filters (start/end)
  - Multi-select dropdowns for bank, product, and location
  - Free-text semantic search (keywords)
  - Output format selection (CSV, JSON, Parquet, XLSX)
  - LLM model selection for summarization
  - Submit button that triggers the API call
  - Download buttons for the exported file and the summary

Data flow:
  User fills filters → clicks Submit → get_reviews() calls GET /reviews on
  the API → API exports to S3 + summarizes → returns (download_url, summary)
  → UI displays the summary and enables the Download button.

Connection to other modules:
  - bankiru.ui.app              — mounts this Blocks instance at /gradio
  - bankiru.ui.choices          — provides static dropdown choices
  - bankiru.ui.foundation_models — provides the LLM model dropdown choices
  - bankiru.config              — provides GET_REVIEWS_URL and DEFAULT_CLOUD_MODEL
"""

from __future__ import annotations

import gradio as gr
import httpx
import logfire

from bankiru.config import get_settings
from bankiru.ui import choices
from bankiru.ui.foundation_models import list_foundation_models


async def get_reviews(
    start_date: str | None,
    end_date: str | None,
    bank: list[str] | None,
    product: list[str] | None,
    location: list[str] | None,
    keywords: str | None,
    file_format: str,
    cloud_model: str,
):
    """Triggered by the Submit button. Calls the API and returns results.

    Builds query parameters from the UI inputs, calls GET /reviews on the
    internal API service, and returns a tuple of (download_url, summary_md)
    that Gradio maps to the download_url_box and summary components.

    The 600s timeout matches the API's potential processing time for large
    exports with LLM summarization.

    Args:
        start_date: Start of date range (YYYYMMDD string or None)
        end_date: End of date range (YYYYMMDD string or None)
        bank: List of selected bank names
        product: List of selected product labels
        location: List of selected city names
        keywords: Free-text semantic search query
        file_format: Export format (csv/json/parquet/xlsx)
        cloud_model: LLM model name for summarization

    Returns:
        Tuple of (download_url, summary_markdown).
        On error, returns ("", error_message).
    """
    settings = get_settings()
    # Build the query parameters dict, filtering out empty/None values
    # so the API doesn't receive spurious empty filters.
    raw_params: dict[str, object] = {
        "startDate": start_date,
        "endDate": end_date,
        "bankName": bank,
        "product": product,
        "location": location,
        "keywords": keywords,
        "outputFormat": file_format,
        "cloudModel": cloud_model,
    }
    params = {
        k: v
        for k, v in raw_params.items()
        if v is not None and v != "" and v != []
    }

    with logfire.span("ui.get_reviews -> {url}", url=settings.GET_REVIEWS_URL):
        # Call the API service over the internal Docker compose network.
        # The 600s timeout is generous because the API may need to:
        # 1. Query the database, 2. Export to S3, 3. Run LLM summarization.
        async with httpx.AsyncClient(timeout=600.0) as http:
            try:
                response = await http.get(settings.GET_REVIEWS_URL, params=params)
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPError as exc:
                logfire.warning("API call failed: {exc}", exc=str(exc))
                return "", f"Error talking to the API: {exc}"

    # Return the pre-signed download URL and the LLM summary.
    return body.get("url") or "", body.get("comment") or "(no comment returned)"


# Module-scope component declarations: NOT auto-rendered (only declarations
# inside `with gr.Blocks():` are). We render() them explicitly below.
#
#   * download_url_box — hidden textbox carrying the most recent pre-signed
#     download URL. visible=False keeps it out of layout but, unlike
#     gr.State, its value DOES round-trip to the browser — required because
#     the Download button uses `fn=None, js=...` which only sees client-
#     side component values.
#   * summary — declared up here so the Clear button (placed in the middle
#     column, BEFORE the summary slot in the right column) can reference it.
download_url_box = gr.Textbox(value="", visible=False)
summary = gr.Markdown(
    height=490,
    buttons=["copy"],
    container=True,
    padding=True,
    # Stable DOM id for mount-time CSS in app.py (summary bottom padding tweak).
    elem_id="summary-panel",
)


# ── Gradio Blocks layout ────────────────────────────────────────────────────
# The layout is a three-column design:
#   Left column (scale=4):  filter inputs (dates, bank, product, location, semantic search)
#   Middle column (scale=4): format/model selection, action buttons
#   Right column (scale=7):  summary display (Markdown with copy button)
#
# fill_height=True makes the Blocks expand to fill the browser viewport.
with gr.Blocks(title="bankiru-reviews", fill_height=True) as gradio_ui:
    # Render the hidden download URL box (declared above) into the layout.
    # It must be inside the Blocks context to participate in event wiring.
    download_url_box.render()

    # Page title
    gr.HTML(
        "<p style=\"text-align:left;\">"
        "Banki.ru Claims and Negative Reviews</p>"
    )
    with gr.Row(height=40):
        pass  # visual spacer between the title and the main controls

    # ── Main three-column layout ─────────────────────────────────────
    with gr.Row(equal_height=True):
        # ── Left column: filter inputs ───────────────────────────────
        with gr.Column(scale=4):
            # Date range: type="string" returns YYYYMMDD strings that
            # the API's Request model can parse.
            start_date = gr.DateTime(
                label="Start",
                type="string",
                include_time=False
            )
            end_date = gr.DateTime(
                label="End",
                type="string",
                include_time=False
            )
            # Bank filter: multi-select dropdown with top-50 banks.
            # Default: "Сбербанк" (the largest Russian bank by complaints).
            bank = gr.Dropdown(
                label="Bank",
                choices=choices.BANKS,
                multiselect=True,
                value="Сбербанк",
            )
            # Product filter: multi-select dropdown with all banking products.
            product = gr.Dropdown(
                label="Product",
                choices=choices.PRODUCTS,
                multiselect=True,
                value=None,
            )
            # Location filter: multi-select dropdown with Russian regional capitals.
            location = gr.Dropdown(
                label="Location",
                choices=choices.LOCATIONS,
                multiselect=True,
                value=None,
            )
            # Semantic search: free-text query that is embedded and compared
            # against review embeddings via pgvector cosine similarity.
            keywords = gr.Textbox(
                label="Semantic search",
                lines=1,
                placeholder="Describe what you're looking for...",
                value=None,
            )

        # ── Middle column: format, model, and action buttons ─────────
        with gr.Column(scale=4):
            # Export format selection (CSV, JSON, Parquet, XLSX).
            file_format = gr.Dropdown(
                label="Format",
                choices=choices.FILE_FORMATS,
                value="parquet",
            )
            # LLM model selection for summarization. The choices are
            # fetched from the Cloud.ru Foundation Models catalog (cached).
            cloud_model = gr.Dropdown(
                label="Summary model",
                choices=list_foundation_models(),
                value=get_settings().DEFAULT_CLOUD_MODEL,
            )

            # Collect all input components into a list for event wiring.
            # This list is passed as `inputs` to submit.click() and as
            # `components` to ClearButton.
            inputs = [
                start_date, end_date, bank, product, location, keywords,
                file_format, cloud_model
            ]

            # Primary action button: triggers the API call.
            submit = gr.Button(value="Submit", variant="primary")
            # Clear button: resets all inputs, the summary, and the download URL.
            gr.ClearButton(
                components=inputs + [summary, download_url_box],
                value="Clear",
            )
            # Download buttons: trigger client-side JavaScript (no server round-trip).
            download_reviews_btn = gr.Button(value="Download reviews")
            download_summary_btn = gr.Button(value="Download summary")

        # ── Right column: summary display ────────────────────────────
        with gr.Column(scale=7):
            with gr.Accordion("Summary"):
                # Render the summary Markdown component (declared above).
                summary.render()

    # ── Event wiring ─────────────────────────────────────────────────
    # Submit button: call get_reviews() with all inputs, write results
    # to the hidden download_url_box and the visible summary component.
    # On success, show a toast notification prompting the user to download.
    submit.click(
        fn=get_reviews,
        inputs=inputs,
        outputs=[download_url_box, summary],
    ).success(
        lambda: gr.Info("Download your file", duration=5)
    )

    # Open the most recent pre-signed S3 URL in a new tab. No server hop —
    # the browser fetches the file directly from OBS.
    download_reviews_btn.click(
        fn=None,
        inputs=[download_url_box],
        js="(url) => { if (url) { window.open(url, '_blank'); } "
           "else { alert('Click Submit first to generate a file.'); } }",
    )

    # Download the summary as a Markdown file. The filename stem is taken
    # from the most recent pre-signed URL (same as the reviews file) with
    # the extension replaced by .md.
    download_summary_btn.click(
        fn=None,
        inputs=[download_url_box, summary],
        js="(url, summary) => { "
           "if (!summary) { alert('No summary available. Click Submit first.'); return; } "
           "var name = 'summary.md'; "
           "if (url) { try { var base = new URL(url).pathname.split('/').pop(); "
           "name = base.includes('.') ? base.replace(/\\.[^.]+$/, '.md') : base + '.md'; "
           "} catch(e) {} } "
           "var blob = new Blob([summary], {type: 'text/markdown'}); "
           "var a = document.createElement('a'); "
           "a.href = URL.createObjectURL(blob); "
           "a.download = name; "
           "a.click(); "
           "URL.revokeObjectURL(a.href); }",
    )
