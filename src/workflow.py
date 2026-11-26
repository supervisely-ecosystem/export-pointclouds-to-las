"""Workflow lineage: the input project and the output files of this app."""

import supervisely as sly


def workflow_input(api: sly.Api, project_id: int) -> None:
    try:
        api.app.workflow.add_input_project(int(project_id))
    except Exception as e:
        sly.logger.debug(f"Failed to add the input to the workflow: {e!r}")


def workflow_output(api: sly.Api, file: sly.api.file_api.FileInfo, folder: str) -> None:
    try:
        relation_settings = sly.WorkflowSettings(
            title=file.name,
            icon="archive",
            icon_color="#33c94c",
            icon_bg_color="#d9f7e4",
            url=f"/files/{file.id}/true/?teamId={file.team_id}",
            url_title="Download",
        )
        meta = sly.WorkflowMeta(relation_settings=relation_settings)
        api.app.workflow.add_output_file(file, meta=meta)
        sly.logger.debug(f"Workflow: output {folder}")
    except Exception as e:
        sly.logger.debug(f"Failed to add the output to the workflow: {e!r}")
