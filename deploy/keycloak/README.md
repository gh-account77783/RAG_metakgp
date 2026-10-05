# GraphMind identity service candidate

GraphMind uses Keycloak 26.7.3 as its only credential, account, grant, token,
verification, and identity-link authority. PostgreSQL 17 is the selected
same-host production database candidate. Keycloak's development-file database is
not accepted for a release installation.

The realm template is configuration input, not a secret-bearing deployment
artifact. Materialize it into a private run directory after replacing its
environment placeholders. The `GRAPHMIND_IDENTITY_*` placeholders deliberately
match the application environment names; the same generated value configures
each client at both ends. Never commit the rendered file. The Google provider
is disabled in the template so an incomplete client configuration cannot be
exposed accidentally.
The template explicitly defines its `email` and minimal `profile` scopes; do not
assume realm import creates referenced built-in scopes. Email/verified-email and
subject mappers must reach access tokens and introspection, and both browser and
MCP clients must have the three declared default scopes assigned on read-back.
This does not add first/last-name profile requirements.
The resource audience mapper must use only `included.custom.audience`, equal to
the public MCP URL. Do not also set `included.client.audience`: Keycloak 26.7.3
gives that field precedence and omits the custom URL, so GraphMind correctly
rejects the resulting token. Read-back alone is insufficient; verify the actual
authorization-code token through introspection against the exact public URL.
Use a separate `graphmind-introspection-client-audience` mapper for the
`graphmind-resource` client audience: Keycloak requires that audience for this
resource client's introspection to return active. Both audiences must reach the
same real access token; never combine their settings in one mapper.

Before integration testing, the host operator must:

1. Set the public GraphMind URL and matching MCP resource URL.
2. Generate independent random secrets for the web, resource-introspection, and
   host-admin clients.
3. Import the realm, then apply `graphmind-user-profile.json` through the private
   administration console's User profile JSON editor or
   `PUT /admin/realms/graphmind/users/profile`. Read that endpoint back and verify
   the configuration before enabling public signup. Realm import alone is not
   sufficient: the default profile requires first/last names and does not manage
   GraphMind's security attributes. The supplied profile requires email, uses
   email as username through the realm setting, and makes security attributes
   administrator-only; unmanaged attributes remain disabled. Profile setup needs
   the bootstrap operator's realm-management permission, not the application's
   host-admin service account. Then grant only `query-users`, `view-users`, and
   `manage-users` from `realm-management` to the `graphmind-admin` service
   account. Do not grant realm administration.
4. Configure SMTP, verify the sender, and leave `Verify email` enabled.
5. Configure Google's exact Keycloak broker callback, then enable the provider.
6. Keep the Keycloak administration console and PostgreSQL on host-private
   interfaces. Only the realm's required public OIDC endpoints pass through the
   HTTPS proxy.
7. Restrict anonymous dynamic client registration. Claude Code uses the
   pre-registered public PKCE client `graphmind-claude-code` and callback
   `http://localhost:8765/callback` for the MVP.

Deleting a GraphMind account is implemented as a retained record with
`graphmind_status=deleted`, `enabled=false`, a deletion timestamp, an incremented
credential epoch, and revoked sessions. The host `restore` command is the only
MVP path back from that state. The Keycloak account-deletion capability must stay
disabled because physical deletion conflicts with the accepted retention rule.

Account CLI mutations are serialized by a host-local lock and read back after
update and session revocation. Do not bypass them with concurrent console/API
user edits. Test deleted -> disable -> enable denial, repeated delete, explicit
restore, security-attribute persistence across restart, and attempted reader
attribute edits against real Keycloak. These assets have offline contract tests;
real profile application and registration remain integration gates.
The JSON deliberately omits `unmanagedAttributePolicy`: disabled is the default.
The pinned 26.7.3 REST parser rejects the literal UI label `DISABLED`; its
[configuration enum](https://raw.githubusercontent.com/keycloak/keycloak/26.7.3/core/src/main/java/org/keycloak/representations/userprofile/config/UPConfig.java)
only represents enabled/admin-view/admin-edit overrides. Profile PUT returns
200 with the normalized configuration, which must still be read back and checked.
The reader process must not receive `GRAPHMIND_IDENTITY_ADMIN_CLIENT_SECRET`.
Use a separate private host-account CLI environment. Listing uses paginated
`first`/`max` requests rather than Keycloak's default first 100 users and rejects
an explicit 10,000-account overflow/repeated page. Pagination follows the pinned
[Admin REST API](https://www.keycloak.org/docs-api/26.7.3/rest-api/index.html#_users).

Before testing, follow the [same-candidate acceptance matrix](../acceptance/README.md).
It records profile read-back, real persistence/email/Google/two-user/client
behavior, proxy logging and failed/pending cases separately from offline tests.

The profile endpoint and `prompt=create` signup initiation follow the pinned
[Keycloak admin implementation](https://raw.githubusercontent.com/keycloak/keycloak/26.7.3/services/src/main/java/org/keycloak/services/resources/admin/UserProfileResource.java)
and [authorization implementation](https://raw.githubusercontent.com/keycloak/keycloak/26.7.3/services/src/main/java/org/keycloak/protocol/oidc/endpoints/AuthorizationEndpoint.java).

Exact production installation, service wrapping, PostgreSQL backup, TLS proxy,
and native Windows acceptance remain P7 work.

## Password recovery candidate (qualification required)

The baseline realm deliberately keeps `resetPasswordAllowed=false`. Enabling
the stock reset flow alone is insufficient: Keycloak's
[password update form](https://github.com/keycloak/keycloak/blob/26.7.3/themes/src/main/resources/theme/base/login/password-commons.ftl)
makes signing out other sessions optional, and the stock reset flow can create
a password for an account without one. GraphMind's accepted policy requires
revocation and leaves Google-only recovery with Google.

`recovery-src/` supplies two small native-authority providers, pinned to 26.7.3:
the existing-password chooser denies disabled/deleted/credentialless accounts
with the same native acknowledgement and uses Keycloak's atomic single-use
cache for a 60-second per-account email cooldown; the password-event listener
advances the credential epoch, sets user not-before and removes online/offline grants in the credential-update
transaction. A revocation failure marks that transaction rollback-only. Neither
provider stores passwords, sends mail independently, logs reset URLs, or adds a
second identity authority. The selected flow retains MFA credentials and forces
a fresh login after reset; it does not reset a lost second factor.
The listener also rolls back a self-service password update if the account was
disabled/deleted after the reset form opened, before its submission. Private
operator credential changes are separate from this self-service eligibility
check; they never clear retained deletion or enable an account automatically.

Build with a verified JDK 21 and the exact installed Keycloak distribution's
`lib/lib/main` jars, without Maven or a system Java installation:

```text
python tools/build_keycloak_recovery.py --jdk-bin /private/jdk21/bin --classpath-dir /private/keycloak/lib/lib/main --output /private/fresh-provider-output
```

The source and builder ship as operator assets; a precompiled provider is not
bundled. In an installed wheel these assets are below `share/graphmind/` in the
installation prefix. Output includes a deterministic JAR and source/library
SHA-256 inventory. Copy the JAR to the selected Keycloak's `providers/` directory
and rebuild/restart that installation using its documented service procedure.
Never install into an unrelated existing Keycloak or enable recovery before
qualification. Changing Keycloak versions requires rebuilding and retesting.

`graphmind-recovery.json` is a separate activation policy: create its
`authenticatorConfig` and `flow`, preserve the realm's existing listeners while
adding `eventListener`, then apply `realmSettings` through private host
administration. The reset link lifespan is 900 seconds. Read back providers,
flow requirements, force-login, listeners and lifespan before exposing the
setting. Verify real reset without checking the optional logout box, old online
and offline access/refresh denial, new-password login, token tamper/expiry/reuse,
unknown/disabled/deleted/Google-only denial, email failure and abuse controls.
Also test an account disabled/deleted after link issuance. Qualify the supplied
`nginx-recovery.conf.example` public POST rate limit in addition to the per-account cooldown, and rerun
callback/reset-link log canaries. A private SMTP sink is not real mailbox or
public HTTPS acceptance; an owner-controlled mailbox reset is still required.

This candidate is not a completed recovery or release gate. Keep Google
disabled until separate broker configuration and acceptance pass.

## Clear account emails

`themes/graphmind/email` overrides only six English message strings: reset and
verification subjects and their plain-text/HTML bodies. It inherits Keycloak's
native `base` email templates and HTML sanitizer, without custom template code,
images, tracking, shortened links or changes to identity handling. The subjects
identify GraphMind and the action; bodies explain why the email arrived, use a
clear action link and explain what to do if the request was not theirs.

Copy the `graphmind` theme directory to the selected Keycloak installation's
`themes/` directory, then set **only Email theme** to `graphmind` through private
realm administration (`emailTheme`). Leave login/account/admin themes alone.
The stock realm template does not select this optional theme before installation.
Both bodies use native link `{0}`, realm name `{2}` and formatted expiry `{3}`;
reset's 15-minute lifespan and verification's configured lifespan remain native
settings, not text constants. Never replace, log or shorten the actual links.

Qualify both MIME alternatives against native Keycloak mail rendered to an
isolated SMTP sink, exercise both links, and check unchanged security settings
and secret-free logs before selection on a test realm. Existing mail is not
rewritten. English is the only customized language; broader localization is
unqualified. Improved wording does not establish inbox placement: sender
authentication, reputation and recipient filtering still apply.
