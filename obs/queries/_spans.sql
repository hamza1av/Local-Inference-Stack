-- Prelude for every query in this folder: flattens the recorder spool (OTLP/JSON lines,
-- one ExportTraceServiceRequest per line) into one row per span.
--
-- The spool location comes from $SPOOL_GLOB (set by `just obs::spool`), e.g.
--   SPOOL_GLOB="$STACK_DATA/spool/traces*.jsonl" duckdb -c ".read obs/queries/_spans.sql" -c "from spans limit 5"
--
-- app follows the replayer's rules: pk-lf-<name> from the captured public key
-- (not for UUID-style keys), else service.name, else 'unknown'.
CREATE OR REPLACE TEMP MACRO otlp_attrs(arr) AS
  map_from_entries(list_transform(
    coalesce(CAST(arr AS JSON[]), []),
    lambda a: {'k': a->>'key',
          'v': coalesce(a->'value'->>'stringValue', a->'value'->>'intValue',
                        a->'value'->>'doubleValue', a->'value'->>'boolValue',
                        CAST(a->'value'->'arrayValue'->'values' AS VARCHAR),
                        CAST(a->'value'->'kvlistValue' AS VARCHAR))}));

CREATE OR REPLACE TEMP VIEW spans AS
WITH lines AS (
  SELECT json AS j FROM read_ndjson_objects(getenv('SPOOL_GLOB'), ignore_errors = true)
), rs AS (
  SELECT unnest(CAST(j->'resourceSpans' AS JSON[])) AS r FROM lines
), ss AS (
  SELECT r, unnest(CAST(r->'scopeSpans' AS JSON[])) AS s FROM rs
), sp AS (
  SELECT otlp_attrs(r->'resource'->'attributes') AS res, s->'scope'->>'name' AS scope,
         unnest(CAST(s->'spans' AS JSON[])) AS x
  FROM ss
), flat AS (
  SELECT
    x->>'traceId'                                   AS trace_id,
    x->>'spanId'                                    AS span_id,
    nullif(x->>'parentSpanId', '')                  AS parent_id,
    x->>'name'                                      AS name,
    scope,
    make_timestamp(CAST(CAST(x->>'startTimeUnixNano' AS HUGEINT) // 1000 AS BIGINT)) AS start_ts,
    (CAST(x->>'endTimeUnixNano' AS HUGEINT) - CAST(x->>'startTimeUnixNano' AS HUGEINT)) / 1e6 AS duration_ms,
    coalesce(CAST(x->'status'->>'code' AS VARCHAR), '0') AS status_code,
    x->'status'->>'message'                         AS status_message,
    res,
    otlp_attrs(x->'attributes')                     AS attrs
  FROM sp
)
SELECT
  *,
  CASE
    WHEN regexp_matches(coalesce(res['langfuse.ingest.public_key'], ''), '^pk-lf-.+')
     AND NOT regexp_matches(res['langfuse.ingest.public_key'], '^pk-lf-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-')
      THEN regexp_replace(res['langfuse.ingest.public_key'], '^pk-lf-', '')
    WHEN res['service.name'] IS NOT NULL AND NOT starts_with(res['service.name'], 'unknown_service')
      THEN res['service.name']
    ELSE 'unknown'
  END AS app,
  coalesce(attrs['gen_ai.response.model'], attrs['gen_ai.request.model'], attrs['langfuse.observation.model.name']) AS model,
  CAST(coalesce(attrs['gen_ai.usage.input_tokens'], attrs['gen_ai.usage.prompt_tokens']) AS BIGINT)      AS input_tokens,
  CAST(coalesce(attrs['gen_ai.usage.output_tokens'], attrs['gen_ai.usage.completion_tokens']) AS BIGINT) AS output_tokens,
  coalesce(attrs['langfuse.observation.type'] = 'generation'
    OR attrs['gen_ai.request.model'] IS NOT NULL
    OR attrs['gen_ai.operation.name'] IN ('chat', 'text_completion', 'generate_content'), false) AS is_generation,
  coalesce(status_code IN ('2', 'STATUS_CODE_ERROR') OR attrs['langfuse.observation.level'] = 'ERROR', false) AS is_error
FROM flat;
