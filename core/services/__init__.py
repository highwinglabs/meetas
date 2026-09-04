"""Domain mixins of the MeetingService (see core/service.py).

Each module holds one business domain as a mixin class. All methods stay
exactly where the original code executed: MeetingService composes them via
the MRO, so call semantics, locking and error behaviour are unchanged.
"""
