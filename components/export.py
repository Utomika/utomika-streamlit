import streamlit as st

import resources


def render_export(client, labeler):
    with st.sidebar.expander("Export my labels"):
        if st.button("Load my labels as CSV"):
            export = resources.export_df(client, labeler)
            if export is not None:
                st.download_button(
                    "Download labels.csv",
                    data=export.to_csv(index=False),
                    file_name="utomika_labels.csv", mime="text/csv",
                )
            else:
                st.caption("You haven't labeled any scenes yet.")
