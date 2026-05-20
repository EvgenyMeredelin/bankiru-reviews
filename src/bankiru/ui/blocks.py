"""Gradio UI for browsing/filtering/summarizing the bankiru reviews dataset."""

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
    file_format: str,
    cloud_model: str,
):
    """Triggered by Submit. Calls the API and returns `(download_url, summary_md)`."""
    settings = get_settings()
    raw_params: dict[str, object] = {
        "startDate": start_date,
        "endDate": end_date,
        "bankName": bank,
        "product": product,
        "location": location,
        "outputFormat": file_format,
        "cloudModel": cloud_model,
    }
    params = {
        k: v
        for k, v in raw_params.items()
        if v is not None and v != "" and v != []
    }

    with logfire.span("ui.get_reviews -> {url}", url=settings.GET_REVIEWS_URL):
        async with httpx.AsyncClient(timeout=600.0) as http:
            try:
                response = await http.get(settings.GET_REVIEWS_URL, params=params)
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPError as exc:
                logfire.warning("API call failed: {exc}", exc=str(exc))
                return "", f"Error talking to the API: {exc}"

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
    height=390,
    buttons=["copy"],
    container=True,
    padding=True,
)


with gr.Blocks(title="Banki.ru UI", fill_height=True) as gradio_ui:
    download_url_box.render()

    gr.HTML(
        "<p style=\"text-align:left;\">"
        "Banki.ru Claims and Negative Reviews</p>"
    )
    with gr.Row(height=40):
        pass  # visual spacer between the title and the main controls
    with gr.Row(equal_height=True):
        with gr.Column(scale=4):
            start_date = gr.DateTime(label="Start", type="string", include_time=False)
            end_date = gr.DateTime(label="End", type="string", include_time=False)
            bank = gr.Dropdown(
                label="Bank",
                choices=choices.BANKS,
                multiselect=True,
                value="Сбербанк",
            )
            product = gr.Dropdown(
                label="Product",
                choices=choices.PRODUCTS,
                multiselect=True,
                value=None,
            )
            location = gr.Dropdown(
                label="Location",
                choices=choices.LOCATIONS,
                multiselect=True,
                value=None,
            )
        with gr.Column(scale=4):
            file_format = gr.Dropdown(
                label="Format",
                choices=choices.FILE_FORMATS,
                value="parquet",
            )
            cloud_model = gr.Dropdown(
                label="Cloud model",
                choices=list_foundation_models(),
                value=get_settings().DEFAULT_CLOUD_MODEL,
            )

            inputs = [start_date, end_date, bank, product, location, file_format, cloud_model]

            submit = gr.Button(value="Submit", variant="primary")
            gr.ClearButton(
                components=inputs + [summary, download_url_box],
                value="Clear",
            )
            download_reviews_btn = gr.Button(value="Download reviews")
            download_summary_btn = gr.Button(value="Download summary")
        with gr.Column(scale=7):
            with gr.Accordion("Summary"):
                summary.render()

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
