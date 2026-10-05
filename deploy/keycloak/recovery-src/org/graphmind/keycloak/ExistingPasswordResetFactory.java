package org.graphmind.keycloak;

import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.Authenticator;
import org.keycloak.authentication.authenticators.resetcred.ResetCredentialChooseUser;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.UserModel;

/** Recovery cannot enable deleted accounts or silently add a Google-only password. */
public final class ExistingPasswordResetFactory extends ResetCredentialChooseUser {
    public static final String ID = "graphmind-reset-existing-password";

    private void guard(AuthenticationFlowContext context) {
        UserModel user = context.getUser();
        if (user == null) return;
        String status = user.getFirstAttribute("graphmind_status");
        boolean active = status == null || "active".equalsIgnoreCase(status);
        if (!user.isEnabled() || !active || user.getFirstAttribute("graphmind_deleted_at") != null
                || !user.credentialManager().isConfiguredFor("password")) {
            // Native email execution gives the same generic acknowledgement for
            // unknown/disabled/deleted/Google-only users; no credential is created.
            context.clearUser();
        }
    }

    @Override public void authenticate(AuthenticationFlowContext context) {
        super.authenticate(context);
        guard(context);
    }

    @Override public void action(AuthenticationFlowContext context) {
        super.action(context);
        guard(context);
        UserModel user = context.getUser();
        if (user != null && !context.getSession().singleUseObjects().putIfAbsent(
                "graphmind-reset-cooldown:" + context.getRealm().getId() + ":" + user.getId(), 60)) {
            // Atomic, bounded native Keycloak cache; no address/token/password log.
            // Only the chooser POST is limited, never the email-link continuation.
            context.clearUser();
        }
    }

    @Override public Authenticator create(KeycloakSession session) { return new ExistingPasswordResetFactory(); }
    @Override public String getId() { return ID; }
    @Override public String getDisplayType() { return "GraphMind existing-password recovery"; }
    @Override public String getHelpText() { return "Recover only enabled, active existing-password accounts; preserve broker-only identities and retained deletion."; }
}
