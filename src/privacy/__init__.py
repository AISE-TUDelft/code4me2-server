"""Account-level privacy controls (GDPR): data-collection consent and erasure.

``collection`` decides whether data about an account may be stored at all and
records an opt-out; ``erasure`` removes what was collected. Every collection
path asks ``collection`` rather than trusting a client flag. Like the research
stores, both modules take a caller-managed SQLAlchemy session and never commit,
so the router owns the unit of work.
"""
