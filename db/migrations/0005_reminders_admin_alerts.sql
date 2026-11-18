-- 0005_reminders_admin_alerts.sql: deadline reminders and admin alerts.
--
-- Reminders use the existing unique index (profile_id, deadline_id, kind), with kinds like
-- 'reminder_7d'. A row is inserted with status 'pending' BEFORE the email is sent, so two
-- runs can never both send it; it becomes 'sent' after sending, or is deleted if sending
-- failed (so the next run tries again).
--
-- Admin alerts go to ADMIN_EMAIL, not to a profile, so profile_id may now be empty for them.
-- alert_key names the problem (e.g. 'source_failing:nust-pg'). While the problem lasts, its
-- row has resolved_at = null and the unique index below stops a second alert. When the
-- problem is gone, resolved_at is set, which "re-arms" the alert for the next time.

alter table notifications alter column profile_id drop not null;

alter table notifications
    add column alert_key   text,
    add column resolved_at timestamptz,
    add constraint notifications_profile_or_admin
        check (kind = 'admin' or profile_id is not null),
    add constraint notifications_admin_has_key
        check (kind <> 'admin' or alert_key is not null);

create unique index notifications_one_open_alert_idx
    on notifications (alert_key)
    where kind = 'admin' and resolved_at is null;
