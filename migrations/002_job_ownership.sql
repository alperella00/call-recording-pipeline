-- Which worker is processing a job and since when. Ownership itself is a session-level advisory lock held
-- by that worker's database connection (see app/queue.py); these columns only make it visible.

ALTER TABLE jobs
    ADD COLUMN worker_id  text,
    ADD COLUMN started_at timestamptz;
