python3 eval_real_repos.py \
  --repo requests/src/requests \
  --check ./sessions.py:Session.send --verbose

=== Relevance check: ./sessions.py:Session.send ===
  Direct callees (5):
    → ./sessions.py:Session.get_adapter
    → ./hooks.py:dispatch_hook
    → ./cookies.py:extract_cookies_to_jar
    → ./utils.py:resolve_proxies
    → ./sessions.py:SessionRedirectMixin.resolve_redirects
  Blast radius (8) — who breaks if this changes:
    ← ./sessions.py:Session.delete
    ← ./sessions.py:Session.get
    ← ./sessions.py:Session.head
    ← ./sessions.py:Session.options
    ← ./sessions.py:Session.patch
    ← ./sessions.py:Session.post
    ← ./sessions.py:Session.put
    ← ./sessions.py:Session.request

  Total context: 44 functions
    ./_internal_utils.py:to_native_string
    ./auth.py:_basic_auth_str
    ./cookies.py:cookiejar_from_dict
    ./cookies.py:create_cookie
    ./cookies.py:extract_cookies_to_jar
    ./cookies.py:merge_cookies
    ./hooks.py:dispatch_hook
    ./sessions.py:Session.delete
    ./sessions.py:Session.get
    ./sessions.py:Session.get_adapter
    ./sessions.py:Session.head
    ./sessions.py:Session.merge_environment_settings
    ./sessions.py:Session.options
    ./sessions.py:Session.patch
    ./sessions.py:Session.post
    ./sessions.py:Session.prepare_request
    ./sessions.py:Session.put
    ./sessions.py:Session.request
    ./sessions.py:Session.send
    ./sessions.py:SessionRedirectMixin.get_redirect_target
    ./sessions.py:SessionRedirectMixin.rebuild_auth
    ./sessions.py:SessionRedirectMixin.rebuild_method
    ./sessions.py:SessionRedirectMixin.rebuild_proxies
    ./sessions.py:SessionRedirectMixin.resolve_redirects
    ./sessions.py:SessionRedirectMixin.send
    ./sessions.py:SessionRedirectMixin.should_strip_auth
    ./sessions.py:merge_hooks
    ./sessions.py:merge_setting
    ./utils.py:address_in_network
    ./utils.py:dotted_netmask
    ./utils.py:get_auth_from_url
    ./utils.py:get_environ_proxies
    ./utils.py:get_netrc_auth
    ./utils.py:get_proxy
    ./utils.py:is_ipv4_address
    ./utils.py:is_valid_cidr
    ./utils.py:proxy_bypass
    ./utils.py:requote_uri
    ./utils.py:resolve_proxies
    ./utils.py:rewind_body
    ./utils.py:set_environ
    ./utils.py:should_bypass_proxies
    ./utils.py:to_key_val_list
    ./utils.py:unquote_unreserved
trakshan@trakshan-HP-Pavilion-Laptop-15-cs3xxx:~/temporary/titanic.csv/diffcopy/diffcontext$ python3 eval_real_repos_v2.py --online django
python3 eval_real_repos_v2.py --online flask
python3 eval_real_repos_v2.py --online black
python3 eval_real_repos_v2.py --online httpx

Cloning https://github.com/django/django ...
Building graph for: /tmp/diffctx_django_1fp49b94/django
  Graph: 8737 nodes, 4099 edges

  Spot check: ./db/models/query.py:QuerySet.filter
    Callees: 2, Blast: 3, Context: 10 fns
    First callee: ./db/models/query.py:QuerySet._not_support_combined_queries
    First in blast: ./db/models/query.py:QuerySet.contains

  Spot check: ./db/models/query.py:QuerySet.count
    Callees: 0, Blast: 0, Context: 1 fns

  Top 5 hubs (most callers — potential context pollution):
      33 callers: ./contrib/admin/checks.py:must_be
      29 callers: ./db/backends/base/schema.py:BaseDatabaseSchemaEditor.quote_name
      23 callers: ./db/models/query.py:QuerySet._chain
      22 callers: ./db/migrations/autodetector.py:MigrationAutodetector.add_operation
      19 callers: ./db/backends/base/schema.py:BaseDatabaseSchemaEditor.execute

Cloning https://github.com/pallets/flask ...
Building graph for: /tmp/diffctx_flask_keev1t4x/src/flask
  Graph: 332 nodes, 162 edges

  Spot check: ./app.py:Flask.route
    NOT FOUND in graph (fn ID may differ between versions)

  Spot check: ./app.py:Flask.make_response
    Callees: 0, Blast: 5, Context: 20 fns
    First in blast: ./app.py:Flask.__call__

  Top 5 hubs (most callers — potential context pollution):
      10 callers: ./sansio/blueprints.py:Blueprint.record_once
       9 callers: ./app.py:Flask.ensure_sync
       5 callers: ./sansio/scaffold.py:Scaffold._method_route
       4 callers: ./helpers.py:get_debug_flag
       3 callers: ./sansio/app.py:App._find_error_handler

Cloning https://github.com/psf/black ...
Building graph for: /tmp/diffctx_black_3u90ecsv/src/black
  Graph: 422 nodes, 371 edges

  Spot check: ./linegen.py:transform_line
    Callees: 7, Blast: 2, Context: 15 fns
    First callee: ./linegen.py:_hugging_power_ops_line_to_string
    First in blast: ./linegen.py:run_transformer

  Spot check: ./mode.py:Mode.__post_init__
    NOT FOUND in graph (fn ID may differ between versions)

  Top 5 hubs (most callers — potential context pollution):
      20 callers: ./linegen.py:LineGenerator.visit_default
      14 callers: ./trans.py:is_valid_index_factory
      14 callers: ./trans.py:is_valid_index
      12 callers: ./linegen.py:LineGenerator.line
       9 callers: ./trans.py:TErr

Cloning https://github.com/encode/httpx ...
Building graph for: /tmp/diffctx_httpx_ixzbc6nm/httpx
  Graph: 424 nodes, 209 edges

  Spot check: ./_client.py:Client.get
    Callees: 1, Blast: 0, Context: 24 fns
    First callee: ./_client.py:Client.request

  Spot check: ./_client.py:Client.send
    Callees: 3, Blast: 9, Context: 31 fns
    First callee: ./_client.py:BaseClient._set_timeout
    First in blast: ./_client.py:Client.delete

  Top 5 hubs (most callers — potential context pollution):
      12 callers: ./_exceptions.py:request_context
       7 callers: ./_client.py:Client.request
       7 callers: ./_client.py:AsyncClient.request
       7 callers: ./_api.py:request
       6 callers: ./_utils.py:to_bytes
trakshan@trakshan-HP-Pavilion-Laptop-15-cs3xxx:~/temporary/titanic.csv/diffcopy/diffcontext$ python3 benchmark_runner.py \
  --repo https://github.com/psf/black \
  --changed ./src/black/linegen.py:transform_line \
  --name black_live
Cloning https://github.com/psf/black to /tmp/tmpjhf1uokn...
Cloning into '/tmp/tmpjhf1uokn'...
remote: Enumerating objects: 15760, done.
remote: Counting objects: 100% (98/98), done.
remote: Compressing objects: 100% (59/59), done.
remote: Total 15760 (delta 66), reused 40 (delta 39), pack-reused 15662 (from 3)
Receiving objects: 100% (15760/15760), 8.09 MiB | 4.35 MiB/s, done.
Resolving deltas: 100% (11064/11064), done.

============================================================
  black_live
  Changed: ['./src/black/linegen.py:transform_line']
============================================================
  Total functions : 638
  Full repo tokens: 141,997

  Retrieved 15 / 638 functions:
    ./src/black/linegen.py:_ensure_trailing_comma
    ./src/black/linegen.py:_first_right_hand_split
    ./src/black/linegen.py:_force_standalone_comment_split
    ./src/black/linegen.py:_hugging_power_ops_line_to_string
    ./src/black/linegen.py:_maybe_split_omitting_optional_parens
    ./src/black/linegen.py:_over_length_only_due_to_subscript_comment
    ./src/black/linegen.py:_prefer_split_rhs_oop_over_rhs
    ./src/black/linegen.py:bracket_split_build_line
    ./src/black/linegen.py:bracket_split_succeeded_or_raise
    ./src/black/linegen.py:generate_trailers_to_omit
    ./src/black/linegen.py:right_hand_split
    ./src/black/linegen.py:run_transformer
    ./src/black/linegen.py:should_split_funcdef_with_rhs
    ./src/black/linegen.py:should_split_line
    ./src/black/linegen.py:transform_line

  Context tokens  : 7,510
  Token reduction : 94.7%
  Fn reduction    : 97.6%
  Runtime         : 2394.8 ms

Saved to benchmark_results.json
trakshan@trakshan-HP-Pavilion-Laptop-15-cs3xxx:~/temporary/titanic.csv/diffcopy/diffcontext$ 