from dataclasses import dataclass


@dataclass
class SessionArtifacts:
    discovery_doc: bytes
    discovery_doc_filename: str
    discovery_doc_mime: str
    claude_md: str
    claude_md_filename: str
