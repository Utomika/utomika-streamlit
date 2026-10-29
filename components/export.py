import streamlit as st

import resources


def render_export(client, labeler):
    with st.sidebar.expander("Export my labels"):
        if st.button("Load my labels as CSV"):
            try:
                export = resources.export_df(client, labeler)
            except Exception as e:  # noqa: BLE001
                st.error("Couldn't export labels — labeling server is unreachable.")
                resources.mark_labels_down(e)
                return
            if export is not None:
                st.download_button(
                    "Download labels.csv",
                    data=export.to_csv(index=False),
                    file_name="utomika_labels.csv", mime="text/csv",
                )
            else:
                st.caption("You haven't labeled any scenes yet.")
