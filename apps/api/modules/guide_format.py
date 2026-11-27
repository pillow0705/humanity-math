"""Canonical public document: JSON graph front matter followed by Markdown."""
import json

MAX_DOCUMENT_CHARS=40000

def render_document(header,body):
    return '---\n'+json.dumps(header,ensure_ascii=False,indent=2)+'\n---\n\n'+body.rstrip()+'\n'
