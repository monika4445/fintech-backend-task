# Test task, Python Backend Developer (FinTech)

**English** | [Русский](README.ru.md)

Three mini tasks: atomic transaction processing in Django, a Celery task that fails on `Decimal`, and nginx with the domain taken from an environment variable. In two of the three the obvious answer is wrong, and most of this document is about that.

| Task | What turned out to be non-obvious |
| --- | --- |
| [1. Atomic transaction processing](#task-1-atomic-transaction-processing) | `select_related` next to `select_for_update` also locks the user row: `FOR UPDATE` without `OF` locks every table in the join. Logins queue up behind batch processing. Guarded by a test on the actual SQL. |
| [2. `Decimal` is not JSON serializable](#task-2-typeerror-object-of-type-decimal-is-not-json-serializable) | The same error text comes from two different places, and they are fixed differently. kombu already carries `Decimal` by itself, so registering a custom serializer is unnecessary and harmful. |
| [3. Domain from an environment variable](#task-3-nginx-with-the-domain-from-an-environment-variable) | Four bugs in the standard recipe that only show up on a running stack. One of them silently disables all config rendering and doesn't log a single line. |

**Where the answers are.** Task 1 is in [`billing/services.py`](billing/services.py) and [`billing/models.py`](billing/models.py). Task 2 is in [`locations/tasks.py`](locations/tasks.py) and [`locations/encoders.py`](locations/encoders.py). Task 3 is in [`docker/nginx/templates/`](docker/nginx/templates) and the `nginx` service in [`docker-compose.yml`](docker-compose.yml). That is about 370 of the roughly 1600 lines in the repository; the rest is an operational layer that doesn't answer the task's questions and is marked below as added on purpose.

Everything starts with one command, 89 tests run against a real PostgreSQL, and every claim below was checked on a running stack rather than inferred from reading the code.

**If you're short on time**, look at the [engineering approach](#engineering-approach), the [optimizations rejected after measurement](#rejected-after-measurement) and [what was deliberately left undone](#left-unresolved-on-purpose). They show how decisions were made, not just which ones.

## How to run

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(64))"   # into SECRET_KEY
docker compose up -d          # db, two redis, migrate, app, worker, nginx, certbot
curl http://localhost/healthz
```

Required variables (`DOMAIN`, `SECRET_KEY`, `POSTGRES_*`) have no defaults in either compose or settings. A missing variable fails startup with a clear message. This is deliberate, see the section on the final review's findings.

Tests run against a real Postgres, in a separate build stage so that test dependencies don't end up in the production image:

```bash
docker compose --profile test run --rm test
```

Locally, without docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
env SECRET_KEY=dev-only DOMAIN=example.com \
    POSTGRES_HOST=127.0.0.1 POSTGRES_DB=app POSTGRES_USER=app POSTGRES_PASSWORD=app \
    CELERY_BROKER_URL=redis://localhost:6379/0 REDIS_URL=redis://localhost:6379/1 \
    python manage.py test --buffer
```

Verified on Django 6.1, Celery 5.6.3, kombu 5.6.2, psycopg 3.3, nginx 1.27.5, Postgres 16, Redis 7. Suite: 89 tests, all green, including checks of the lock scope against the actual SQL, query plans, the task's schema via `information_schema`, and the Redis write from the task itself.

## Data schema

`Profile` and `Transaction` live in `billing/models.py` and follow the task statement. `User` is in `accounts/models.py`, and it is the one place where I had to depart from the obvious solution.

At first this used the stock `django.contrib.auth.User`: its `id`, `username` and `email` fields match the statement, and a custom model looked like giving up ready-made authentication only to rewrite the same thing. The mistake showed up not in the code but in the database. The statement declares `User.id` as `bigint`, while the stock model creates an `integer`: the `auth` app overrides `DEFAULT_AUTO_FIELD` with its own `default_auto_field = AutoField`, and the project setting has no effect on it. Types read from the live database showed `integer(32,0)` not only on `auth_user.id` but also on `profile.user_id` and `transaction.user_id`. A foreign key column copies the type of its target, so one mismatch became three.

By itself this isn't urgent: a 32-bit key tops out at about 2.1 billion records. But changing the user model after the fact is painful in Django, which is why it's recommended to create one at the start of a new project, and here there is no data, so the migration costs nothing. `accounts.User` inherits from `AbstractUser` and overrides only the primary key; it has more fields than the schema, which is a superset, not a mismatch.

The mismatch survived several review passes, because all of them read the code rather than looking at the created tables: `DEFAULT_AUTO_FIELD = BigAutoField` was in place and looked convincing. That's why `tests/test_schema_contract.py` exists. It checks all twelve columns from the statement against `information_schema`, including `varchar(20)`, `decimal(12,2)` and the UNIQUE on `profile.user_id`.

---

## Engineering approach

These are not general principles but decisions made here, together with what was rejected and why. Each one rests on something you can check in this repository.

**Abstractions on demand, not out of habit.** There are no Service, Repository or DAO layers here: the operation is expressed directly with the ORM and fits on one screen. A layer would be worth adding to isolate a changing external dependency, remove noticeable duplication, or get a test boundary. The task creates none of these three reasons, and an extra layer would have to be maintained forever.

**The transaction boundary is visible from the first line.** `@transaction.atomic` sits on the function itself, not in the calling code. The caller doesn't have to remember about atomicity, because whatever has to be remembered eventually gets forgotten.

**A claim about behavior comes with a test.** This isn't style but the result of a specific mistake: a comment saying "only the profile is locked" was wrong, and that comment is what stopped the review, because the reader trusted the text instead of the SQL. Now the lock scope, the index predicate and the query plan are checked by tests rather than described.

**Fail instead of degrading silently.** No `SECRET_KEY` means failing at startup, not starting with a publicly known key. No database address means failing, not writing financial transactions to an unknown place. Passing a `Profile` instead of a `User`, or a string instead of a collection of statuses, raises `TypeError` instead of processing someone else's transactions or returning a silent zero. A renamed nginx template makes the script exit with code 1 instead of bringing up half a config. Silent degradation is exactly the class of failure that neither tests nor monitoring can see.

**Types are handled at the serialization boundary.** The task has no type checks on its input: that's the job of the encoder's `default()`, which is called only for what `json.dumps` can't handle by itself and works at any nesting depth. A new type is added in one place.

**Reverse conversion by schema, not by the shape of the string.** An early version guessed and corrupted data, and this was measured: `'0042'` became `Decimal('42')`, and `'NaN'` became a value not equal to itself. The string `'123'` can be an amount, a postal code or a foreign key; only the caller can tell them apart, so the caller passes the list of fields.

**Configuration is run-time data.** The domain isn't in the image in any form, and that's verified: the image hash is the same before and after changing `DOMAIN`. The same image runs in any environment.

**Optimize only with a number.** Every performance change here rests on a measurement, and four tempting optimizations were rejected by measurement. For example, `only()` gave seven percent instead of the expected tens. The rejected ones are described alongside the accepted ones, because "tried it and dropped it" is also a result.

**What wasn't done and why.** No keepalive to gunicorn gives a hard ceiling of about 470 connections per second, and that was calculated rather than ignored. It isn't fixed by a config change but by a decision about the perimeter architecture, so it is named rather than done in a hurry. In the same way, the docs mark that protection against reordering requires extending the task's contract, and that the list of allowed status transitions is domain knowledge to agree with the business, not to guess in code.

## Task 1. Atomic transaction processing

Code is in `billing/services.py`, tests in `billing/tests/test_services.py`.

```python
@transaction.atomic
def process_user_transactions(
    user: AbstractBaseUser | int,
    *,
    statuses: Collection[str] | None = None,
) -> int:
    user_id = _resolve_user_id(user)
    statuses = _validated_statuses(statuses)

    profile = Profile.objects.select_for_update().get(user_id=user_id)

    queryset = Transaction.objects.select_for_update().filter(user_id=user_id)
    if statuses is not None:
        queryset = queryset.filter(status__in=statuses)
    transactions = queryset.order_by("pk")

    updated = 0
    for tx in transactions.iterator(chunk_size=FETCH_CHUNK_SIZE):
        tx.status = TransactionStatus.PROCESSED
        tx.save(update_fields=["status", "updated_at"])
        updated += 1

    if not profile.has_processed_transactions:
        profile.has_processed_transactions = True
        profile.save(update_fields=["has_processed_transactions"])

    transaction.on_commit(lambda: _notify_processed(user_id, updated))
    return updated
```

### Why it's done this way

**`update_fields` includes `updated_at` explicitly.** This isn't overcaution. Django filters the field list by `update_fields` before calling `pre_save` (`django/db/models/base.py`, line 1090 in 6.1), and `auto_now` is implemented exactly through `pre_save`. Without naming it explicitly, the modification time silently doesn't update, and you'd only notice from discrepancies in reports. Covered by the test `test_updated_at_moves_despite_update_fields`.

**Only the profile row is locked, and this is checked against the SQL.** This deserves a closer look, because the tempting optimization has exactly the opposite effect. `Profile.objects.select_for_update().select_related("user")` looks like it saves one query, but in PostgreSQL `SELECT ... FOR UPDATE` without `OF` locks the rows of **every** table in the join. SQL captured from the live database:

```
SELECT ... FROM "profile" INNER JOIN "auth_user" ON (...) WHERE ... FOR UPDATE
```

So `auth_user` stays locked for the whole update loop, and any login that updates `last_login` waits in line. The function never needs `profile.user`, so there's no join here at all. The test `LockScopeTests.test_profile_lock_does_not_join_the_user_table` pins this by the query text rather than by behavior, because a behavioral test can't see the difference.

**`order_by("pk")` is required.** Without it the planner chooses the row order, two workers on overlapping sets take locks in different orders and deadlock. A common order by primary key makes lock acquisition consistent across all processes. This is exactly the kind of bug that doesn't reproduce in tests and shows up in production under load.

**All transactions are processed, and narrowing is the caller's decision.** The statement requires updating the status of all transactions, and the schema describes `status` simply as `varchar(20)` without any list of values. So by default there is no filter.

The temptation to add a state machine here is strong and correct in spirit: the transitions `refunded → processed` and `failed → processed` mean counting money that was already refunded a second time. But making that the default means that on someone else's data with a status like `new` or `hold`, nothing gets processed and the profile still gets the "processed" flag. A silent zero is worse than no protection. So `PROCESSABLE_STATUSES` is available through the `statuses` parameter, and the list of statuses stays domain knowledge this function doesn't have.

Separately: when zero rows are updated, the flag is still set as the statement requires, but a warning goes to the log. The state "profile marked, zero processed" should be discoverable from logs, not by investigating report discrepancies.

**The side effect is moved into `on_commit`.** Sending an event or a Celery task inside `atomic` publishes a fact that may not happen if the transaction rolls back. Worse, the worker can pick up the task before the commit and not find the data. This is a common production race that looks like intermittent `DoesNotExist` errors.

**A missing profile fails loudly.** The schema guarantees `UNIQUE` on `user_id` but doesn't guarantee the row exists. Creating a profile on the fly would hide a broken user registration.

**An index on `(user_id, id)`.** The query filters by `user_id` and orders by `id`. Without an index, that's a seq scan over the whole transactions table under `SELECT FOR UPDATE`, so lock hold time grows with history rather than with the size of the batch being processed. Indexing the pair `(user_id, status)` instead would be a mistake, and that mistake was made here: the analysis is in the section on the partial index below.

### What was added beyond the statement and why

The function accepts both a `User` instance and its primary key. The statement describes it as taking a user, but often the caller has nothing but an id, and there's no reason to make it run an extra query for that. An unsaved object without a `pk` is rejected with a clear error instead of failing a query deeper in the stack.

There is an optional `statuses` parameter. It's not in the statement, and its default matches the statement: all transactions are processed. It exists so the caller can forbid transitions like `refunded → processed`, meaning counting refunded money a second time, without making this function pretend it knows the status list of someone else's system.

The `status` field deliberately has **no** `choices`. The task's schema describes it as `varchar(20)` and doesn't fix a list of values, so declaring `choices` would add an application-level constraint that isn't in the statement, and would contradict the decision to process all transactions. A value like `hold` from another system must be written and processed, not rejected by form validation; this is pinned by a test with `full_clean()`. `TransactionStatus` stays as a set of named constants, so a typo like `proccessed` breaks on import rather than in the data.

### The cost of the "only save()" constraint

Forbidding `bulk_update` means one UPDATE per row. With a 1 ms round trip to the database, that's about a second of lock hold time per thousand transactions, and for all that time competing processes for this user wait. This is a ceiling, not an implementation detail. The code has `LOCK_DURATION_WARNING_ROWS`, which logs a warning when a batch gets dangerously large, so the ceiling is found in the logs rather than in monitoring. With real growth in volume, the right next step is to split the operation into batches, deliberately giving up global atomicity in exchange for bounded lock duration, and that decision isn't the function author's to make.

### The most important thing to know here

**On SQLite this code protects against nothing, and silently.** The SQLite backend has `has_select_for_update = False`, and the query compiler checks `if self.query.select_for_update and features.has_select_for_update` (`django/db/models/sql/compiler.py`, line 840). The condition is false, and `FOR UPDATE` simply doesn't make it into the SQL. No exception, no warning. The tests are green and there's no protection. That's why the suite here runs against Postgres, and the concurrency test is marked `@skipUnlessDBFeature("has_select_for_update")`, so on SQLite it is honestly skipped instead of creating false confidence.

The test `test_concurrent_run_cannot_double_process` starts four threads on one unprocessed transaction and requires the result `[0, 0, 0, 1]`. It passes on Postgres, which means the lock really is taken.

---

## Task 2. `TypeError: Object of type Decimal is not JSON serializable`

Code is in `locations/encoders.py` and `locations/tasks.py`, tests in `locations/tests/test_tasks.py`.

### Diagnosis first, then the fix

The same error text can come from two different places, and they are fixed differently.

In the web process, on `.delay()`, when kombu serializes the task arguments. In that case changing the task body doesn't help at all.

In the worker, when `json.dumps` runs in the task body.

Only the traceback tells them apart, and checking it is the first thing to do. The answer "just add your own encoder" without this fork fixes half the cases.

### A verified fact that changes the answer

Celery's default `"json"` serializer is not `json.dumps` from the standard library but `kombu.utils.json`. Since 5.x it carries `Decimal`, `datetime`, `date`, `time` and `UUID` by itself, through a typed envelope like `{"__type__": "decimal", "__value__": "55.7558"}`, and restores them to the original type on the worker side.

Verified on kombu 5.6.2, test `test_kombu_json_already_handles_decimal_datetime_uuid`: the round trip preserves `Decimal`, the microseconds in `datetime`, and the `UUID` type.

This has two consequences.

First: the producer doesn't fail on `Decimal`, so the error in the statement can only come from the task body. The diagnosis is unambiguous. What's more, precisely because kombu faithfully delivers `Decimal` to the worker, the argument inside the task really is a `Decimal`, which is what a bare `json.dumps` trips over. This is pinned by the test `test_task_body_receives_real_decimal`.

Second, and more important: **registering your own content type through `kombu.serialization.register` is unnecessary and harmful here.** The benefit is zero, and the cost is real: it's a breaking change during rollout. Workers that don't have the new type in `accept_content` reject messages with `ContentDisallowed`, and tasks are lost during the deploy window. If such a migration is ever needed (an old kombu, custom types), it has to ship in two releases: first `accept_content` is extended everywhere, and only in the next release is `task_serializer` switched. Rollback is symmetric and also two-phase. This is written in `config/celery.py` next to the decision not to do it.

### The fix

```python
@celery_app.task(max_retries=3, autoretry_for=(RedisConnectionError, RedisTimeoutError))
def send_location_update(user_id: int, location_data: dict) -> None:
    payload = dumps(location_data)                     # ExtendedJSONEncoder
    redis_client.get_redis().set(
        f"user:{user_id}:last_loc", payload, ex=LAST_LOCATION_TTL_SECONDS
    )
```

The signature matches the original. There's deliberately no `bind=True`: it would add a `self` parameter and so change the signature, which the statement explicitly forbids. Retries don't suffer from this: `autoretry_for` works without binding, and `self` wouldn't be needed in the body anyway.

There are no type checks on the input: types are the job of the encoder's `default()`, which is called only for what `json.dumps` can't handle itself and works at any nesting depth.

### What was added beyond the statement

The write uses a one-day TTL (`ex=`). The statement has no expiry, it just says to write the value. This was added deliberately: Redis is memory, not disk, and a per-user key without an expiry lives forever, including for users who left a year ago. With a million users that's hundreds of megabytes that are never freed. The downside is stated plainly: after a day without updates the location disappears, and the consumer has to be ready for that.

Retries on Redis network errors were also added. They don't touch the signature: `autoretry_for` works without `bind=True`, which is what made it possible to do without a `self` parameter.

### Why a custom encoder and not `DjangoJSONEncoder`

`DjangoJSONEncoder` solves the same problem in one line, but it truncates microseconds to milliseconds (`if o.microsecond: r = r[:23] + r[26:]`) and rewrites the `+00:00` suffix to `Z`. For coordinates that doesn't matter; for financial timestamps it's an irreversible loss that nobody notices until the first reconciliation. The difference is pinned by the test `test_microseconds_survive_unlike_djangojsonencoder`.

`Decimal` goes out as a string, not a `float`. `float(Decimal("0.1"))` can't be reversed, and for money amounts that's a direct loss of precision.

The module depends only on the standard library, because it's imported before Django's app registry is ready.

### What else was wrong with this task

**A key without a TTL.** Redis is memory, not disk. A per-user key without an expiry lives forever, including for users who left a year ago. With a million users that's hundreds of megabytes that are never freed.

**The Redis client is created once per process** (`locations/redis_client.py`) and reset on the `worker_process_init` signal. `Redis.from_url` on every task call means a new TCP connection for every message and hitting the file descriptor limit long before Redis's own limit. The reset after fork is needed separately: a socket opened in the parent process would be shared by all children of a prefork worker, and two processes would read each other's replies from one descriptor, mixed together.

**Reverse conversion by a list of fields, not by the shape of the string.** `restore_decimals(data, fields=("lat", "lon"))` returns `Decimal` only for the fields the caller named. The first version guessed: it tried `Decimal(value)` on every string and swapped the type on success. That can't be done, and this isn't a theoretical objection but measured data corruption:

```
'0042'  -> Decimal('42')     leading zeros lost in a card number
'01234' -> Decimal('1234')   corrupted postal code
'1e5'   -> Decimal('1E+5')   an external identifier became a number
'NaN'   -> Decimal('NaN')    a value not equal to itself
```

The last case is the nastiest: `Decimal('NaN') != Decimal('NaN')`, so any comparison further down the stack starts silently returning False. Guessing the type from the shape of a string is impossible in principle: `'123'` can be an amount, a postal code or a foreign key, and only the schema the caller knows can tell them apart.

**Reordering was found, but it can't be closed within the statement.** Celery doesn't guarantee delivery order, and `autoretry_for` makes the danger worse: a retry of an old task overwrites a fresh location with an older one, silently and without an error.

A protection was written, a Lua script that atomically compares the incoming data's version with the stored one, and then removed. The reason is that there's nowhere to get the version from without breaking the statement. It can't be passed as an argument, because the signature is fixed. Taking it from the payload by keys like `timestamp`, `ts` and so on would mean assigning meaning to keys of someone else's dictionary, which by the statement can be anything, and in someone else's data `ts` may well not mean time. The cost of such a mistake is a silently dropped legitimate update, exactly the class of failure the protection was meant to prevent.

The right solution is to extend the contract: the version arrives as an explicit argument or a message header. That's a change to the requirements, not the implementation, so here it's named rather than faked.

---

## Task 3. Nginx with the domain from an environment variable

Configs are in `docker/nginx/`, orchestration in `docker-compose.yml`.

Since version 1.19 the official nginx image runs `/etc/nginx/templates/*.template` through `envsubst` at container start and puts the result into `/etc/nginx/conf.d/`. No custom entrypoint and no image rebuild. The templates are mounted as a volume rather than copied into the image; otherwise the domain binding would end up in an image layer and a rebuild would be needed again.

Changing the domain means editing `.env` plus `docker compose up -d --force-recreate nginx app`. Verified: the image hash doesn't change before and after changing `DOMAIN`.

### Four bugs in the standard recipe, found by running it

These aren't visible when reading the config and only show up on a live stack.

**`include /etc/letsencrypt/options-ssl-nginx.conf` and `ssl_dhparam` don't exist.** These files are created by certbot's nginx plugin, and when issuing with `certonly --webroot` they aren't there. nginx with a missing include doesn't start at all. So the TLS parameters are set explicitly in the template.

**`certbot renew` doesn't reload nginx.** The certificate is renewed on disk, but nginx keeps the old one in memory. After 90 days the site serves an expired certificate while auto-renewal is formally working, and you find out from users. A `--deploy-hook` can't reach across containers, so the periodic reload lives in the nginx container itself (`docker/nginx/entrypoint.d/50-reload-on-cert-renewal.sh`).

**Overriding `command` disables all templating, silently.** `/docker-entrypoint.sh` opens its initialization block only when `[ "$1" = "nginx" ]`. Any `command: sh -c "..."` puts `sh` there, the gate doesn't open, the templates aren't rendered, and nginx starts on the default config. Not a single error line in the logs; the symptom is a 404 instead of proxying. This is exactly what I tripped over when I attached the reload loop through `command`. So `command` isn't overridden, and everything extra lives in `/docker-entrypoint.d/`.

**A static `upstream` sticks to the container's old IP.** nginx resolves the name in `upstream django { server app:8000; }` once at startup and keeps the address until restart. After `docker compose up -d app` the container gets a new IP, and nginx returns 502 on every request: `connect() failed (113: Host is unreachable) ... upstream: 172.18.0.5:8000`. So a normal application deploy takes the site down until the proxy is restarted by hand. The solution is a `resolver` with the address of Docker's built-in DNS plus `proxy_pass` through a variable, which forces resolution at runtime.

Verified by forcing an IP change (the application moved from `172.18.0.4` to `172.18.0.7`, nginx wasn't restarted): the response stayed 200.

The tradeoff here is deliberate: with a variable in `proxy_pass` the keepalive pool to gunicorn is lost, and each request opens a new TCP connection inside the docker network. That's fractions of a millisecond versus a 502 on every deploy. And `$request_uri` in `proxy_pass` is required: with a variable, nginx doesn't substitute the matched part of the URI automatically, and without it every request would go to the root.

**Auto-renewal was hidden behind a compose profile.** The `certbot` service sat under `profiles: ["tls"]`, while the instructions documented startup as `docker compose up -d`. Compose doesn't start services in a profile without an explicit flag, so following the documented instructions, certbot never started. The reload loop in nginx worked fine, there was just nothing to reload. The profile was removed.

### What compose consists of and what's beyond the task

The statement describes the original `docker-compose.yml` as "the application (Django + Gunicorn) and Nginx". There are nine services here, split as follows.

These answer the task: `app` (Django under Gunicorn), `nginx`, `db` (the first task can't be checked without a database) and `redis-cache` (the second task writes specifically to Redis).

These were added deliberately, each with a reason. `redis-broker` is separate from the cache because their eviction policies are opposite: the cache needs `allkeys-lru`, and for the broker that would mean silently lost messages. `worker` is there so the second task can be checked through a real queue, not only by a call in the same process. `migrate` is a separate one-off run, otherwise `--scale app=3` would run migrations from three containers at once. `certbot` renews the certificate; without it, auto-renewal from the third task exists only on paper. `test` is a separate build stage so test dependencies don't end up in the production image.

You can check the third task's solution by reading only `nginx`, `app` and the templates directory; the rest has nothing to do with it.

### The first-run deadlock

On first run there's no certificate yet, nginx with `ssl_certificate` pointing at a missing file doesn't start, and without a working nginx certbot can't pass the webroot check. This is the most often forgotten part of the task: formally the requirement "don't rebuild the image when DOMAIN changes" is met without it, but in practice changing the domain causes downtime instead of a switchover.

`docker/nginx/entrypoint.d/40-select-tls-mode.sh` runs after envsubst and picks one of two ready templates: `10-http-redirect.conf` when a certificate exists and `10-http-direct.conf` when it doesn't. The ACME challenge is available in both modes, the certificate gets issued, and a restart turns on HTTPS.

Previously the same effect was achieved by editing the neighboring config with `sed`, tied to the indentation and the exact wording of a line. Any reformatting of the template would have turned it into a silent no-op with exit code 0, and port 80 would redirect to a non-existent HTTPS. Two separate files remove this class of error entirely, and the script also fails with a non-zero code if the expected file isn't there after envsubst.

### How to issue the certificate the first time

The `certbot` service in `docker-compose.yml` only runs `certbot renew`, and `renew` does nothing when there are no certificates. The first issuance is a separate one-off command, and without it the system stays HTTP-only forever.

```bash
# 1. Bring up the stack. There's no certificate, so nginx starts in HTTP-only
#    mode and serves the ACME challenge over HTTP.
docker compose up -d

# 2. Issue the certificate. The domain comes from the same DOMAIN.
#    --entrypoint is required: the service's default entrypoint is `sh -c "<renew
#    loop>"`, and without overriding it the arguments below would land in the
#    script's $0/$1, be silently ignored, and the container would go into the
#    renewal loop.
docker compose run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" -d "www.$DOMAIN" \
    --email you@example.com --agree-tos --no-eff-email

# 3. Restart nginx: the mode is picked once at container start,
#    so a reload won't help here.
docker compose up -d --force-recreate nginx
```

After that the `certbot` service renews the certificate by itself, and nginx re-reads it every six hours (`docker/nginx/entrypoint.d/50-reload-on-cert-renewal.sh`).

The domain has to resolve to this server, otherwise Let's Encrypt won't pass the check. That's an ACME requirement, not a quirk of the configuration.

### The domain isn't only nginx

Django's `ALLOWED_HOSTS` is built from the same `DOMAIN`. Without that you get a 400 Bad Request that looks like an application bug.

`SECURE_PROXY_SSL_HEADER` together with `proxy_set_header X-Forwarded-Proto` is needed so that `request.is_secure()` tells the truth and the secure flags on the session and csrf cookies get set.

`SECURE_SSL_REDIRECT` in Django, on the other hand, is deliberately off, and that's also a finding from the stack. The redirect to https belongs to exactly one layer, and that layer is nginx. Duplicated in Django, it breaks first-run mode: nginx is up HTTP-only and proxies directly, while Django still returns a 301 to https that nobody is listening on yet. Verified: changing `DOMAIN` to a domain without a certificate gave a 301 to a dead address until ownership of the redirect was reduced to one layer.

`SecurityMiddleware` is added to `MIDDLEWARE`. Without it, `SECURE_PROXY_SSL_HEADER` and the neighboring settings are just variables nobody reads. A setting that does nothing is worse than a missing one: it gives false confidence in code review.

### What was verified on a live stack

| Check | Result |
| --- | --- |
| HTTP on the known domain | 301 to `https://localhost/healthz` |
| HTTPS through nginx into Django | 200, `secure: true`, `forwarded_proto: https` |
| ACME challenge over HTTP | 200 in both modes, no redirect |
| Request with a foreign Host on 80 | connection closed (444) |
| Request with a foreign Host on 443 | 400 from `ALLOWED_HOSTS` |
| `/nginx-health` via default_server | 200 |
| Changing `DOMAIN` | image not rebuilt, new domain served |
| Domain without a certificate | HTTP-only, proxying works, ACME reachable |
| Changing the app container's IP | 200 without restarting nginx |
| Renaming a template | script fails with code 1, not silently |

---

## What the final gate found

A separate pass over all the code, assuming failure by default, found 18 problems, six of which had been introduced by earlier fixes. That is a result in itself: the round that declared itself converged was wrong. Here's what's worth knowing about this code.

**An optimization introduced a bug, and a comment hid it.** `select_related` next to `select_for_update` extended the lock to `auth_user`, and the comment claimed exactly the opposite. Hence the rule now followed across the whole repository: if a comment claims a behavior, there must be a test next to it pinning that behavior. For locks, that's a test on the SQL text, because a behavioral test can't see the difference.

**The configuration wasn't checked at all, and almost all the defects lived there.** Secrets were baked into the image through `COPY . .` without a `.dockerignore`, `SECRET_KEY` had a publicly known default, a forgotten `POSTGRES_HOST` silently switched the app to SQLite, the Celery broker ran with `allkeys-lru` and evicted messages, the container ran as root, and with `DEBUG=0` the app didn't log a single error. No ordinary test catches this, so `tests/test_configuration.py` was added: it reads the `Dockerfile`, `.dockerignore` and `docker-compose.yml` and checks them as a contract.

**Every fix here is failing instead of degrading silently.** No secret means failing at startup. No database address means failing instead of writing to a file that dies with the container. Passing a `Profile` instead of a `User` raises `TypeError` instead of processing a stranger's transactions. A renamed nginx template makes the script fail with code 1 instead of bringing up a half-built config. Silent degradation is exactly the class of failure that tests and monitoring can't see.

---

## Optimization: what was measured and what was changed

A separate pass where every change rests on a measurement rather than intuition. The measurements rejected four tempting optimizations, and they are described below alongside the accepted ones.

**Streaming instead of materializing the set.** Iterating a queryset the usual way loaded all rows into memory before the loop started. Measured on 100,000 rows: a peak of 69.9 MiB versus 1.4 MiB with `iterator(chunk_size=2000)`, which is 98 percent less. Scaled to a million rows, that's 699 MiB versus 14 MiB. Speed isn't the point here: without this a large enough client can't be processed at all, the worker gets OOM-killed in the middle of an open transaction and after the rollback hits the same limit again.

**An index on `(user_id, id)`, and why the partial version had to be rolled back.** At first there was a partial index here: the query asks only for unprocessed transactions, so there's no reason to index terminal ones. The measurements looked convincing: 144 kB versus 1288 kB, nine times smaller, and the query at 2.49 ms versus 3.32 ms.

It was a regression. There isn't one path to the `user_id` column but at least two: besides the batch query, there's the cascading delete of a user, which looks up by `user_id` with no status filter at all. Its plan degraded to a `Seq Scan` over the whole table, measured at 100,000 rows filtered out for nothing. For fintech, deleting an account isn't hypothetical but a requirement for deleting personal data.

The mistake wasn't in the index but in the method: one query was measured, and the conclusion was drawn about the column. Now there's a plain index on `(user_id, id)` that covers both paths, and both are covered by plan tests.

**The automatic ForeignKey index was removed.** It was a strict prefix of the composite one and so redundant: 1280 kB and an extra index update on every write, while this function does one UPDATE per row.

**Index migrations use CONCURRENTLY.** A regular `CREATE INDEX` holds SHARE on the table and blocks writes, and `DROP INDEX` takes ACCESS EXCLUSIVE and blocks everything. On a running service's transactions table that's minutes of downtime inside a normal deploy. The cost of `atomic = False` is stated in the migration itself.

**A connection pool instead of persistent connections.** It used to be `psycopg2` with `CONN_MAX_AGE`, so the number of connections equaled workers times replicas, and a PostgreSQL connection is an OS process of about ten megabytes. Now it's `psycopg3` with Django's native pool. Django forbids combining the pool with persistent connections, so `CONN_MAX_AGE` has to be zero here; that's a backend requirement, not an oversight. `DB_POOL=0` is left for the case where PgBouncer sits in front.

**gthread instead of the sync model.** The app waits on Postgres and Redis rather than computing, but three sync workers served exactly three requests at a time. The thread count matches the pool size: Django connections are thread-local, and without a pool this change would multiply the load on the database instead of reducing it. Order matters here, and the two can't be changed separately.

**The worker restart threshold went from 1000 to 10000.** Measured: `django.setup()` costs 153 ms, and at a hundred requests per second per worker, the old threshold restarted it every ten seconds, losing about two percent of capacity to guard against a leak this code doesn't have.

**The worker healthcheck no longer starts an interpreter or touches the broker.** It used to be `celery inspect ping`: measured at 0.47–0.64 s per call because of the full Django and Celery import, plus a broadcast request that all workers handle, so the service traffic grows quadratically with the number of containers. Now it's a file heartbeat. The limitation is stated honestly in the module itself: this checks that the process is alive, and a worker that's alive but disconnected from the queue looks healthy from here; the other half is covered by a queue depth metric, not a healthcheck.

**Compression and rate limiting at the perimeter.** `gzip` wasn't enabled at all, verified: 6000 bytes versus 42 after enabling it. `limit_req` was missing; now one client can't take the whole worker pool's capacity, verified on the stack: six 200 responses, then 429.

**Static files with hashed names.** A permanent cache on stable names meant that some users worked with old static files for a month after a deploy. With `ManifestStaticFilesStorage` the name contains a hash of the content, so `immutable` becomes correct.

**Structured logs, and two Celery traps along the way.** First: `worker_hijack_root_logger` is `True` by default, so Celery replaces the root logger's handlers and the whole `LOGGING` block is ignored inside the worker; half the system would write text while the other half writes JSON. The second showed up right after fixing the first: `celery.worker.strategy` prints the full list of task arguments at INFO, and the record contained `"args": "(11, {'lat': Decimal('1'), 'ts': 5000})"`. Those are user coordinates, meaning personal data leaking into the log collection system for its whole retention period. Both loggers are turned down to WARNING.

**Other.** The Redis pool has an upper bound, containers got memory and CPU limits, `COPY --chown` replaced a separate `chown -R` that duplicated a layer, `curl` was removed from the image in favor of the standard `urllib`, and `PROCESSABLE_STATUSES` became a tuple for a stable order of values in `IN` and consistent statistics in `pg_stat_statements`.

### Rejected after measurement

This is the most useful part, because three of the four looked convincing.

Narrowing the query to `only("pk", "status")` is pointless: 736 bytes per row versus 682, a seven percent saving. The model object's overhead dominates the field values, and in return you get deferred fields, where accessing one silently makes an extra query. `iterator()` solves the same problem and gives 98 percent.

Adding `id` to the composite index to remove the sort node doesn't work: measured, the `Sort` stays. `status IN (...)` with several values breaks the ordering guarantee by `id`. The gain that was attributed to the column order at the time actually came from the index being partial, the same partiality that later had to be rolled back as a regression.

Deferring the Celery import for startup time gives nothing: `django.setup()` costs 153 ms, and adding `import config` adds nothing within the noise.

Reusing an encoder instance instead of `cls=` in `json.dumps` is a micro-optimization below the noise level and not worth the loss in readability.

### Left unresolved on purpose

Handling a full broker isn't written because there's nothing to attach it to: the production code has no `.delay()` call at all. The statement asked for a task, not calling code built into the application. Adding a view or a signal just to demonstrate the handling would mean inventing requirements for the author.

The row-level lock on transactions is held together with the profile lock, even though the profile is what serves as the mutex. Removing it is a decision, not an edit: it needs an answer to whether the table has other writers, and that knowledge isn't inside the task.

The nginx rate limit counts clients by `$binary_remote_addr` and behind a load balancer would see one address. This is fixed with the `real_ip_header` directive together with a list of trusted ranges, and the list depends on what sits in front of nginx. Adding the header without the ranges would be worse than adding nothing: any client could then spoof its address and bypass the limit.

There's no keepalive pool to gunicorn, because `proxy_pass` through a variable is incompatible with an `upstream` block, and the variable was chosen for resilience to the container's IP changing on deploy. The cost is measured and it's hard: the container has 28,232 ephemeral ports with `tcp_fin_timeout` 60, so the ceiling is about 470 new connections per second, after which 502s start while the application's metrics look clean. This isn't fixed by a config change: service discovery has to be handed to a layer built for it (an orchestrator with a stable VIP, Traefik, Caddy), and only then can `upstream` with `keepalive` come back. The decision about the perimeter architecture is outside the scope of this pass, so it's named rather than done in a hurry.
