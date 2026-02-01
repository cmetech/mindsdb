from http import HTTPStatus

from flask import request, current_app as ca
from flask_restx import Resource

from mindsdb.api.http.namespaces.configs.projects import ns_conf
from mindsdb.api.http.utils import http_error
from mindsdb.metrics.metrics import api_endpoint_metrics

from mindsdb.interfaces.jobs.jobs_controller import parse_job_date
from mindsdb.utilities.exception import EntityNotExistsError, JobLockedException


# Allowed internal services for job endpoints (OSCAR-Kore Integration)
# NOTE: Middleware is included because it overwrites X-Internal-Service to 'middleware' in
# PROXY_HEADERS when proxying requests. So even though scheduler sets the header, by the
# time requests reach Kore, the header value is 'middleware'. This is belt-and-suspenders
# validation; middleware already blocked unauthorized callers via require_internal_service.
ALLOWED_INTERNAL_SERVICES = {"scheduler", "middleware"}


def _validate_internal_service():
    """Validate caller is an allowed internal service.

    Returns:
        Error response tuple or None if valid
    """
    internal_service = request.headers.get("X-Internal-Service")
    if not internal_service:
        return http_error(HTTPStatus.FORBIDDEN, "Internal only", "This endpoint requires X-Internal-Service header")
    if internal_service not in ALLOWED_INTERNAL_SERVICES:
        return http_error(
            HTTPStatus.FORBIDDEN,
            "Unauthorized service",
            f"Service '{internal_service}' is not authorized for this endpoint",
        )
    return None


@ns_conf.route("/<project_name>/jobs")
class JobsResource(Resource):
    @ns_conf.doc("list_jobs")
    @api_endpoint_metrics("GET", "/jobs")
    def get(self, project_name):
        """List all jobs in a project"""
        return ca.jobs_controller.get_list(project_name)

    @ns_conf.doc("create_job")
    @api_endpoint_metrics("POST", "/jobs")
    def post(self, project_name):
        """Create a job in a project"""

        # Check for required parameters.
        if "job" not in request.json:
            return http_error(HTTPStatus.BAD_REQUEST, "Missing parameter", 'Must provide "job" parameter in POST body')

        job = request.json["job"]

        name = job.pop("name")
        if job["start_at"] is not None:
            job["start_at"] = parse_job_date(job["start_at"])
        if job["end_at"] is not None:
            job["end_at"] = parse_job_date(job["end_at"])

        create_job_name = ca.jobs_controller.add(name, project_name, **job)

        return ca.jobs_controller.get(create_job_name, project_name)


@ns_conf.route("/<project_name>/jobs/<job_name>")
@ns_conf.param("project_name", "Name of the project")
@ns_conf.param("job_name", "Name of the job")
class JobResource(Resource):
    @ns_conf.doc("get_job")
    @api_endpoint_metrics("GET", "/jobs/job")
    def get(self, project_name, job_name):
        """Gets a job by name"""
        job_info = ca.jobs_controller.get(job_name, project_name)
        if job_info is not None:
            return job_info

        return http_error(HTTPStatus.NOT_FOUND, "Job not found", f"Job with name {job_name} does not exist")

    @ns_conf.doc("delete_job")
    @api_endpoint_metrics("DELETE", "/jobs/job")
    def delete(self, project_name, job_name):
        """Deletes a job by name"""
        ca.jobs_controller.delete(job_name, project_name)

        return "", HTTPStatus.NO_CONTENT


@ns_conf.route("/<project_name>/jobs/<job_name>/history")
@ns_conf.param("project_name", "Name of the project")
@ns_conf.param("job_name", "Name of the job")
class JobsHistory(Resource):
    @ns_conf.doc("job_history")
    @api_endpoint_metrics("GET", "/jobs/job/history")
    def get(self, project_name, job_name):
        """Get history of job calls"""
        if ca.jobs_controller.get(job_name, project_name) is None:
            return http_error(HTTPStatus.NOT_FOUND, "Job not found", f"Job with name {job_name} does not exist")

        return ca.jobs_controller.get_history(job_name, project_name)


# ============================================================================
# Job Management Endpoints (OSCAR-Kore Integration)
# ALL INTERNAL-ONLY - require X-Internal-Service header with allowed value
# ============================================================================


@ns_conf.route("/jobs/pending")
class JobsPending(Resource):
    @ns_conf.doc("list_pending_jobs")
    @api_endpoint_metrics("GET", "/jobs/pending")
    def get(self):
        """List jobs ready for execution (INTERNAL-ONLY for scheduler)"""
        error = _validate_internal_service()
        if error:
            return error

        limit = request.args.get("limit", 100, type=int)
        return ca.jobs_controller.get_pending_jobs(limit=limit)


@ns_conf.route("/jobs/<int:job_id>")
class JobById(Resource):
    @ns_conf.doc("get_job_by_id")
    @api_endpoint_metrics("GET", "/jobs/by_id")
    def get(self, job_id):
        """Get job details by numeric ID (INTERNAL-ONLY)"""
        error = _validate_internal_service()
        if error:
            return error

        try:
            return ca.jobs_controller.get_by_id(job_id)
        except EntityNotExistsError:
            return http_error(HTTPStatus.NOT_FOUND, "Job not found", f"Job {job_id} does not exist")


@ns_conf.route("/jobs/<int:job_id>/execute")
class JobExecute(Resource):
    @ns_conf.doc("execute_job")
    @api_endpoint_metrics("POST", "/jobs/execute")
    def post(self, job_id):
        """Execute a job by ID (INTERNAL-ONLY for scheduler)"""
        error = _validate_internal_service()
        if error:
            return error

        try:
            return ca.jobs_controller.execute_by_id(job_id)
        except EntityNotExistsError:
            return http_error(HTTPStatus.NOT_FOUND, "Job not found", f"Job {job_id} does not exist")
        except JobLockedException as e:
            return http_error(HTTPStatus.LOCKED, "Job locked", str(e))


@ns_conf.route("/jobs/<int:job_id>/pause")
class JobPause(Resource):
    @ns_conf.doc("pause_job")
    @api_endpoint_metrics("POST", "/jobs/pause")
    def post(self, job_id):
        """Pause a job (INTERNAL-ONLY)"""
        error = _validate_internal_service()
        if error:
            return error

        try:
            return ca.jobs_controller.pause(job_id)
        except EntityNotExistsError:
            return http_error(HTTPStatus.NOT_FOUND, "Job not found", f"Job {job_id} does not exist")


@ns_conf.route("/jobs/<int:job_id>/resume")
class JobResume(Resource):
    @ns_conf.doc("resume_job")
    @api_endpoint_metrics("POST", "/jobs/resume")
    def post(self, job_id):
        """Resume a paused job (INTERNAL-ONLY, schedules from NOW)"""
        error = _validate_internal_service()
        if error:
            return error

        try:
            return ca.jobs_controller.resume(job_id)
        except EntityNotExistsError:
            return http_error(HTTPStatus.NOT_FOUND, "Job not found", f"Job {job_id} does not exist")
