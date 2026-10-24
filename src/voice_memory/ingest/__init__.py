"""Audio ingestion: validate -> transcribe -> summarize/tag -> store in SQLite.

Acquiring files (from ReSpeaker Clip or elsewhere) is deliberately out of scope here:
`pipeline.process_file` takes any local audio file.
"""
