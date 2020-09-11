# SPDX-License-Identifier: Apache-2.0
#
# http://nexb.com and https://github.com/nexB/scancode.io
# The ScanCode.io software is licensed under the Apache License version 2.0.
# Data generated with ScanCode.io is provided as-is without warranties.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# Data Generated with ScanCode.io is provided on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, either express or implied. No content created from
# ScanCode.io should be considered or used as legal advice. Consult an Attorney
# for any legal advice.
#
# ScanCode.io is a free software code scanning tool from nexB Inc. and others.
# Visit https://github.com/nexB/scancode.io for support and download.

import json

from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.http import FileResponse
from django.http import StreamingHttpResponse

from rest_framework import mixins
from rest_framework import renderers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from scancodeio import SCAN_NOTICE
from scancodeio import __version__ as scancodeio_version
from scanpipe.api.serializers import CodebaseResourceSerializer
from scanpipe.api.serializers import DiscoveredPackageSerializer
from scanpipe.api.serializers import ProjectErrorSerializer
from scanpipe.api.serializers import ProjectSerializer
from scanpipe.api.serializers import RunSerializer
from scanpipe.models import CodebaseResource
from scanpipe.models import DiscoveredPackage
from scanpipe.models import Project
from scanpipe.models import ProjectError
from scanpipe.models import Run
from scanpipe.pipelines import get_pipeline_description

scanpipe_app_config = apps.get_app_config("scanpipe")


class PassThroughRenderer(renderers.BaseRenderer):
    media_type = ""

    def render(self, data, **kwargs):
        return data


class ResultsGenerator:
    """
    Return `project` results as a generator.
    This allow to stream those results from the database to the client browser
    without having to load everything in memory first.

    Note that the Django Serializer class can output to a stream but cannot be
    sent directly to a StreamingHttpResponse.
    The results would have to be streamed to a file first, then iterated by the
    StreamingHttpResponse, which do not work great in a HTTP request context as
    the request can timeout while the file is generated.
    """

    def __init__(self, project):
        self.project = project

    def __iter__(self):
        yield "{\n"
        yield from self.serialize(label="headers", generator=self.get_headers)
        yield from self.serialize(label="packages", generator=self.get_packages)
        yield from self.serialize(label="files", generator=self.get_files, latest=True)
        yield "}"

    def serialize(self, label, generator, latest=False):
        yield f'"{label}": [\n'

        prefix = ",\n"
        first = True

        for entry in generator(self.project):
            if first:
                first = False
            else:
                entry = prefix + entry
            yield entry

        yield "]\n" if latest else "],\n"

    @staticmethod
    def encode(data):
        return json.dumps(data, indent=2, cls=DjangoJSONEncoder)

    def get_headers(self, project):
        runs = RunSerializer(
            project.runs.all(), many=True, exclude_fields=("url", "project")
        )

        headers = {
            "tool_name": "scanpipe",
            "tool_version": scancodeio_version,
            "notice": SCAN_NOTICE,
            "uuid": project.uuid,
            "created_date": project.created_date,
            "input_files": project.input_files,
            "runs": runs.data,
            "extra_data": project.extra_data,
        }
        yield self.encode(headers)

    def get_packages(self, project):
        discovered_packages = project.discoveredpackages.all()

        for obj in discovered_packages.iterator():
            yield self.encode(DiscoveredPackageSerializer(obj).data)

    def get_files(self, project):
        codebase_resources = project.codebaseresources.exclude(
            type=CodebaseResource.Type.SYMLINK
        ).prefetch_related("discovered_packages")

        for obj in codebase_resources.iterator():
            yield self.encode(CodebaseResourceSerializer(obj).data)


class ProjectViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    A viewset that provides ability to list, get, create, and destroy projects.
    Multiple actions are available to manage project instances.
    """

    queryset = Project.objects.all()
    serializer_class = ProjectSerializer

    @action(detail=True, renderer_classes=[renderers.JSONRenderer])
    def results(self, request, *args, **kwargs):
        """
        Return the results compatible with ScanCode data format.
        The content is returned as a stream of JSON content using the
        ResultsGenerator.
        """
        project = self.get_object()
        results_generator = ResultsGenerator(project)
        return StreamingHttpResponse(
            streaming_content=results_generator, content_type="application/json"
        )

    @action(
        detail=True, name="Results (download)", renderer_classes=[PassThroughRenderer]
    )
    def results_download(self, request, *args, **kwargs):
        """
        Return the results as an attachment.
        The content is streamed using the ResultsGenerator.
        """
        project = self.get_object()
        results_generator = ResultsGenerator(project)

        response = FileResponse(
            streaming_content=results_generator,
            content_type="application/json",
        )
        response["Content-Disposition"] = f'attachment; filename="{project.name}.json"'
        return response

    @action(detail=False)
    def pipelines(self, request, *args, **kwargs):
        data = {}
        for location, name in scanpipe_app_config.pipelines:
            data[name] = {
                "location": location,
                "description": get_pipeline_description(location).split("\n"),
            }
        return Response(data)

    @action(detail=True)
    def resources(self, request, *args, **kwargs):
        project = self.get_object()
        queryset = CodebaseResource.objects.project(project).prefetch_related(
            "discovered_packages"
        )

        paginated_qs = self.paginate_queryset(queryset)
        serializer = CodebaseResourceSerializer(paginated_qs, many=True)

        return Response(serializer.data)

    @action(detail=True)
    def packages(self, request, *args, **kwargs):
        project = self.get_object()
        queryset = DiscoveredPackage.objects.project(project)

        paginated_qs = self.paginate_queryset(queryset)
        serializer = DiscoveredPackageSerializer(paginated_qs, many=True)

        return Response(serializer.data)

    @action(detail=True)
    def errors(self, request, *args, **kwargs):
        project = self.get_object()
        queryset = ProjectError.objects.project(project)

        paginated_qs = self.paginate_queryset(queryset)
        serializer = ProjectErrorSerializer(paginated_qs, many=True)

        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def file_content(self, request, *args, **kwargs):
        project = self.get_object()
        path = request.query_params.get("path")
        codebase_resources = CodebaseResource.objects.project(project)

        try:
            codebase_resource = codebase_resources.get(path=path)
        except CodebaseResource.DoesNotExist:
            message = {"status": "Resource not found. Use ?path=<resource_path>"}
            return Response(message, status=status.HTTP_400_BAD_REQUEST)

        try:
            file_content = codebase_resource.file_content
        except OSError:
            message = {"status": "File not available"}
            return Response(message, status=status.HTTP_400_BAD_REQUEST)

        return Response({"file_content": file_content})

    @action(detail=True, methods=["get", "post"])
    def add_pipeline(self, request, *args, **kwargs):
        project = self.get_object()

        pipeline = request.data.get("pipeline")
        if pipeline:
            if scanpipe_app_config.is_valid(pipeline):
                project.add_pipeline(pipeline)
                return Response({"status": "Pipeline added."})
            message = {"status": f"{pipeline} is not a valid pipeline."}
            return Response(message, status=status.HTTP_400_BAD_REQUEST)

        message = {
            "status": "Pipeline required.",
            "pipelines": [location for location, _ in scanpipe_app_config.pipelines],
        }
        return Response(message, status=status.HTTP_400_BAD_REQUEST)


class RunViewSet(mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    This viewset provides the `detail` only action.
    """

    queryset = Run.objects.all()
    serializer_class = RunSerializer

    @action(detail=True, methods=["get"])
    def start_pipeline(self, request, *args, **kwargs):
        run = self.get_object()
        if run.task_end_date:
            message = {"status": "Pipeline already executed."}
            return Response(message, status=status.HTTP_400_BAD_REQUEST)
        elif run.task_start_date:
            message = {"status": "Pipeline already started."}
            return Response(message, status=status.HTTP_400_BAD_REQUEST)

        transaction.on_commit(lambda: run.run_pipeline_task_async())

        return Response({"status": f"Pipeline {run.pipeline} started."})

    @action(detail=True, methods=["get"])
    def resume_pipeline(self, request, *args, **kwargs):
        run = self.get_object()

        if run.task_succeeded:
            message = {"status": "Cannot resume a successful pipeline run."}
            return Response(message, status=status.HTTP_400_BAD_REQUEST)
        elif not run.task_start_date:
            message = {"status": "Cannot resume never started pipeline run."}
            return Response(message, status=status.HTTP_400_BAD_REQUEST)

        transaction.on_commit(lambda: run.resume_pipeline_task_async())

        return Response({"status": f"Pipeline {run.pipeline} resumed."})
