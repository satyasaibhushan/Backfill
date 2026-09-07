# Product decisions

Backfill is where background work waits, runs, and returns for review.

The queue contains real instructions rather than budget keys. A default task needs
only a name and an outcome. Project, priority, schedule, account preference, allowance,
and permissions are optional choices. The system chooses an account with available
capacity and preserves that choice for later attempts so context stays coherent.

The dashboard has Tasks, Review, Completed, and Settings. Projects filter the task
list. Capacity is two compact cards with Session and Weekly percentages. The tightest
window in each category supplies the displayed value. Raw bucket identifiers are
never product labels. Both providers share the same task and quota controls.

Pause belongs to managing existing work. A task can pause indefinitely or until a date;
all work can pause with the same controls. Resuming all work leaves individual task
pauses intact. Project creation contains a name, optional working folder, and a shared
percentage allowance.

Assumptions for this release:

- One background task at a time prioritizes predictable quota accounting.
- Daily/weekly schedules cover recurring work; arbitrary cron belongs in an external
  scheduler that submits through the API until there is a demonstrated need for more.
- Native permissions stay enabled. Research is the default; editing is explicit.
- Review approval records acceptance. Publishing, merging, and deployment remain
  explicit actions outside the task runner.
- Percentage attribution is estimated account movement. Personal foreground usage may
  therefore reduce a task's remaining allowance. This is safer than pretending native
  tokens map exactly to subscription limits.
- Interrupted work retains its output. Retries are bounded, and a service restart asks
  for review before a potentially duplicated action is attempted again.
