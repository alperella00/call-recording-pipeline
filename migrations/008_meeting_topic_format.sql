-- What a meeting is about and how the caller wants to meet, for the team that prepares it (prompt v3).
-- Older rows (and the rule-based fallback, which can't summarise) have an empty topic.
ALTER TABLE meetings
    ADD COLUMN topic  text NOT NULL DEFAULT '',   -- masked text, like requested_quote
    ADD COLUMN format text NOT NULL DEFAULT 'unknown' CHECK (format IN ('online', 'in_person', 'unknown'));
