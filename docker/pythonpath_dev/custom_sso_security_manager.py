import logging

from superset.security import SupersetSecurityManager


class CustomSsoSecurityManager(SupersetSecurityManager):

    def oauth_user_info(self, provider, response=None):
        me = response["userinfo"]
        if not me.get("name"):
            me["name"] = me["email"] + " " + me["email"]
        first_name = me["name"].split(" ")[0]
        last_name = me["name"].split(" ")[-1]
        logging.debug("user_data: %s", me)
        return {
            "name": me["name"],
            "email": me["email"],
            "username": me.get("preferred_username", me["email"]),
            "first_name": first_name,
            "last_name": last_name,
        }

    def auth_user_oauth(self, userinfo):
        user = self.find_user(username=userinfo["username"])
        if user:
            try:
                user.first_name = userinfo["first_name"]
                user.last_name = userinfo["last_name"]
                user.email = userinfo["email"]
                self.update_user_auth_stat(user)
            except Exception as exc:
                logging.debug("Failed to update user: %s", exc)
        else:
            role = self.find_role(self.auth_user_registration_role)
            user = self.add_user(
                username=userinfo["username"],
                first_name=userinfo["first_name"],
                last_name=userinfo["last_name"],
                email=userinfo["email"],
                role=[role],
            )
            self.update_user_auth_stat(user)
        return user
