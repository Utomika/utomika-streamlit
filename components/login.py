import streamlit as st


# --------------------------------------------------------------------------
# Login gate
# --------------------------------------------------------------------------
def login_gate():
    if st.session_state.get("authed"):
        return True

    st.title("🛰️ UTOMIKA cloud labeling")
    st.caption("Review Landsat overpasses of Zagreb & Split and label each cloudy / clear.")
    with st.form("login"):
        name = st.text_input("Your name / initials", key="login_name")
        pw = st.text_input("App password", type="password")
        ok = st.form_submit_button("Enter")
    if ok:
        if pw == st.secrets.get("APP_PASSWORD"):
            if not name.strip():
                st.error("Please enter your name so labels are attributed.")
                return False
            st.session_state["authed"] = True
            st.session_state["labeler"] = name.strip()
            st.rerun()
        else:
            st.error("Wrong password.")
    return False
