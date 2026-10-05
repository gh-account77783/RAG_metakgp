package org.graphmind.keycloak;

import org.keycloak.Config;
import org.keycloak.common.util.Time;
import org.keycloak.events.Event;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.events.EventType;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.events.admin.OperationType;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;

/** Native-authority password changes revoke online/offline grants, without logs. */
public final class PasswordRevocationFactory implements EventListenerProviderFactory {
    public static final String ID = "graphmind-password-revocation";

    @Override public EventListenerProvider create(KeycloakSession session) {
        return new EventListenerProvider() {
            private void revoke(String realmId, String userId, boolean selfService) {
                try {
                    RealmModel realm = session.realms().getRealm(realmId);
                    if (realm == null) throw new IllegalStateException();
                    UserModel user = session.users().getUserById(realm, userId);
                    if (user == null) throw new IllegalStateException();
                    String status = user.getFirstAttribute("graphmind_status");
                    if (selfService && (!user.isEnabled()
                            || (status != null && !"active".equalsIgnoreCase(status))
                            || user.getFirstAttribute("graphmind_deleted_at") != null))
                        throw new IllegalStateException();
                    String currentEpoch = user.getFirstAttribute("graphmind_credential_epoch");
                    long epoch = currentEpoch == null ? 0 : Long.parseLong(currentEpoch);
                    if (epoch < 0) throw new IllegalStateException();
                    user.setSingleAttribute("graphmind_credential_epoch", Long.toString(Math.addExact(epoch, 1)));
                    session.users().setNotBeforeForUser(realm, user, Time.currentTime());
                    // Keycloak owns this transaction/store. Snapshot streams before
                    // removing sessions; do not call another process/admin API.
                    var online = session.sessions().getUserSessionsStream(realm, user).toList();
                    var offline = session.sessions().getOfflineUserSessionsStream(realm, user).toList();
                    for (var current : online) session.sessions().removeUserSession(realm, current);
                    for (var current : offline) session.sessions().removeOfflineUserSession(realm, current);
                } catch (RuntimeException failure) {
                    // Event dispatch may catch listener errors: explicitly rollback
                    // credential update too. Never commit a reset without revocation.
                    session.getTransactionManager().setRollbackOnly();
                    throw new IllegalStateException("Password revocation failed; transaction rolled back");
                }
            }

            @Override public void onEvent(Event event) {
                if (event.getError() != null || event.getUserId() == null) return;
                boolean password = event.getType() == EventType.UPDATE_PASSWORD
                    || (event.getType() == EventType.UPDATE_CREDENTIAL
                        && event.getDetails() != null
                        && "password".equals(event.getDetails().get("credential_type")));
                if (password) revoke(event.getRealmId(), event.getUserId(), true);
            }

            @Override public void onEvent(AdminEvent event, boolean includeRepresentation) {
                // Never inspect/log representations, which can contain a password.
                if (event.getError() != null || event.getOperationType() != OperationType.ACTION) return;
                String path = event.getResourcePath();
                if (path == null) return;
                String[] parts = path.split("/");
                if (parts.length == 3 && "users".equals(parts[0]) && "reset-password".equals(parts[2]))
                    revoke(event.getRealmId(), parts[1], false);
            }

            @Override public void close() {}
        };
    }

    @Override public String getId() { return ID; }
    @Override public void init(Config.Scope config) {}
    @Override public void postInit(KeycloakSessionFactory factory) {}
    @Override public void close() {}
}
