# Copyright © Boost Organization <boost@boost.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from weblate.boost_endpoint.serializers import AddOrUpdateRequestSerializer
from weblate.boost_endpoint.services import BoostComponentService


class BoostEndpointInfo(APIView):
    """Boost documentation translation API info."""

    permission_classes = (IsAuthenticated,)

    def get(self, request, format=None):  # pylint: disable=redefined-builtin  # noqa: A002
        """Return Boost endpoint module info."""
        return Response(
            {
                "module": "boost-endpoint",
                "description": "Boost documentation translation API",
            }
        )


class AddOrUpdateView(APIView):
    """Add or update Boost documentation components."""

    permission_classes = (IsAuthenticated,)

    def post(self, request, format=None):  # pylint: disable=redefined-builtin  # noqa: A002
        """
        Create or update Boost documentation components.

        add_or_update is a map: lang_code -> [submodule names]. For each lang_code
        the service runs with that language and its submodule list (clone, scan,
        create/update project and components, add language).
        """
        serializer = AddOrUpdateRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        organization = data["organization"]
        add_or_update = data["add_or_update"]
        version = data["version"]
        extensions = data.get("extensions")

        try:
            results = {}
            for lang_code, submodules in add_or_update.items():
                service = BoostComponentService(
                    organization=organization,
                    lang_code=lang_code,
                    version=version,
                    extensions=extensions,
                )
                results[lang_code] = service.process_all(
                    submodules, user=request.user, request=request
                )
        except Exception as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(results, status=status.HTTP_200_OK)
