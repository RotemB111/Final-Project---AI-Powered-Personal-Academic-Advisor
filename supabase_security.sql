-- Locks all tables against direct access from the browser (the public anon/publishable key).
-- The chat server uses the secret key, which bypasses RLS, so the site keeps working.
-- Run once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.

alter table public.students             enable row level security;
alter table public.program              enable row level security;
alter table public.specialization       enable row level security;
alter table public.course               enable row level security;
alter table public.courseoffering       enable row level security;
alter table public.semester_template    enable row level security;
alter table public.chat_sessions        enable row level security;
alter table public.chat_messages        enable row level security;
alter table public.generated_schedules  enable row level security;
alter table public.schedule_classes     enable row level security;
alter table public.studentconstraint    enable row level security;
alter table public.ml_criterion_weights enable row level security;

-- No policies are created on purpose: without a policy, the anon key cannot read or write anything.

-- Check (every row should show rowsecurity = true):
select tablename, rowsecurity from pg_tables where schemaname = 'public' order by tablename;
