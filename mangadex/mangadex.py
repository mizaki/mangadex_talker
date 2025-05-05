"""
MangaDex information source
"""
# Copyright comictagger team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import argparse
import datetime
import json
import logging
import pathlib
import time
from typing import Any, Callable, Generic, TypeVar
from typing_extensions import TypedDict
from urllib.parse import urljoin

import comictalker.talker_utils as talker_utils
import requests
import settngs
from comicapi import utils
from comicapi.genericmetadata import ComicSeries, GenericMetadata, MetadataOrigin
from comicapi.issuestring import IssueString
from comictalker.comiccacher import ComicCacher
from comictalker.comiccacher import Issue as CCIssue
from comictalker.comiccacher import Series as CCSeries
from comictalker.comictalker import ComicTalker, TalkerDataError, TalkerNetworkError
from pyrate_limiter import Duration, Limiter, RequestRate

logger = logging.getLogger(f"comictalker.{__name__}")


class MangaDexTagAttr(TypedDict):
    name: dict[str, str]
    description: dict[str, str]
    group: str
    version: int


class MangaDexTag(TypedDict):
    id: str
    type: str
    attributes: MangaDexTagAttr
    relationships: list


class MangaDexRelScanlationGroupAttr(TypedDict):
    name: str
    altNames: list[dict[str, str]]
    locked: bool
    website: str | None
    ircServer: str | None
    ircChannel: str | None
    discord: str | None
    contactEmail: str | None
    description: str | None
    twitter: str | None
    mangaUpdates: str | None
    focusedLanguages: list
    official: bool
    verified: bool
    inactive: bool
    publishDelay: str | None
    createdAt: str
    updatedAt: str
    version: int


class MangaDexScanlationGroup(TypedDict, total=False):
    id: str
    type: str
    attributes: MangaDexChapterAttr
    relationships: list


class MangaDexChapterAttr(TypedDict):
    volume: str
    chapter: str
    title: str
    image: str  # Not direct from API, generated from volume cover
    translatedLanguage: str
    externalUrl: str | None
    publishAt: str
    readableAt: str
    createdAt: str
    updatedAt: str
    pages: int
    version: int


class MangaDexChapter(TypedDict):
    id: str
    type: str
    attributes: MangaDexChapterAttr
    relationships: list[Any]


class MangaDexSeriesAttr(TypedDict):
    title: dict[str, str]
    altTitles: list[dict[str, str]]
    description: dict[str, str]
    isLocked: bool
    links: dict[str, str]
    originalLanguage: str
    lastVolume: str
    lastChapter: str
    publicationDemographic: str
    status: str
    year: int
    contentRating: str
    tags: list[MangaDexTag]
    state: str
    chapterNumbersResetOnNewVolume: bool
    createdAt: str
    updatedAt: str
    version: int
    availableTranslatedLanguages: list
    latestUploadedChapter: str


class MangaDexSeries(TypedDict, total=False):
    id: str
    type: str
    attributes: MangaDexSeriesAttr
    relationships: list


class MangaDexCoverAttr(TypedDict, total=False):
    description: str
    volume: str
    fileName: str
    locale: str
    createdAt: str
    updatedAt: str
    version: int


class MangaDexCover(TypedDict, total=False):
    id: str
    type: str
    attributes: MangaDexCoverAttr
    relationships: list


class MangaDexError(TypedDict):
    id: str
    status: int
    title: str
    detail: str
    context: str


T = TypeVar("T", MangaDexChapter, list[MangaDexChapter], list[MangaDexSeries], MangaDexSeries, list[MangaDexCover])


class MangaDexResponse(TypedDict, Generic[T], total=False):
    result: str
    errors: list[MangaDexError]
    response: str
    data: T
    limit: int
    offset: int
    total: int


# MangaDex has a limit of 5 calls per second default (https://api.mangadex.org/docs/2-limitations/)
limiter = Limiter(RequestRate(5, Duration.SECOND))


class MangaDexTalker(ComicTalker):
    name: str = "MangaDex"
    id: str = "mangadex"
    comictagger_min_ver: str = "1.6.0a13"
    website: str = "https://mangadex.org"
    logo_url: str = "https://mangadex.org/img/brand/mangadex-logo.svg"
    attribution: str = f"Metadata provided by <a href='{website}'>{name}</a>"
    about: str = (
        f"<a href='{website}'>{name}</a> was created in January 2018 by the former admin and sole developer, "
        f"Hologfx. Since then, MangaDex has been steadily growing, approaching 14 million unique visitors "
        f"per month. The site is currently ran by 21+ unpaid volunteers."
        f"<p>NOTE: Some major series will be missing issue information.</p>"
    )

    def __init__(self, version: str, cache_folder: pathlib.Path):
        super().__init__(version, cache_folder)
        # Default settings
        self.default_api_url = self.api_url = "https://api.mangadex.org"
        self.cover_url_base = "https://uploads.mangadex.org/covers/"

        # Use same defaults as MangaDex
        self.adult_content: bool = False

        self.exclude_doujin: bool = False
        self.use_volume_cover_matching: bool = False
        self.use_volume_cover_window: bool = False
        self.use_ongoing_issue_count: bool = False
        self.use_series_start_as_volume: bool = False

    def register_settings(self, parser: settngs.Manager) -> None:
        parser.add_setting(
            "--mdex-exclude-doujin",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name='Exclude "doujin" content',
            help='Exclude content marked as "doujin"',
        )
        parser.add_setting(
            "--mdex-adult-content",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name='Include "erotica" and "pornographic" content',
            help='Include content marked as "erotica" and "pornographic"',
        )
        parser.add_setting(
            "--mdex-volume-cover-matching",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name="Use the volume cover for chapters when image matching",
            help="Use the volume cover for the chapter when attempting auto-identification. Otherwise text based only. *Enabling this option will require clearing the cache!*",
        )
        parser.add_setting(
            "--mdex-volume-cover-window",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name="Use the volume cover for chapters in issue window",
            help="Use the volume cover for chapters in the issue selection window. *Enabling this option will require clearing the cache!*",
        )
        parser.add_setting(
            "--mdex-use-ongoing",
            default=False,
            action=argparse.BooleanOptionalAction,
            display_name="Use the ongoing issue count",
            help='If a series is labelled as "ongoing", use the current issue count (otherwise empty)',
        )
        parser.add_setting(
            f"--{self.id}-url",
            default="",
            display_name="API URL",
            help=f"Use the given MangaDex URL. (default: {self.default_api_url})",
        )
        parser.add_setting(f"--{self.id}-key", file=False, cmdline=False)

    def parse_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        settings = super().parse_settings(settings)

        self.adult_content = settings["mdex_adult_content"]
        self.exclude_doujin = settings["mdex_exclude_doujin"]
        self.use_volume_cover_matching = settings["mdex_volume_cover_matching"]
        self.use_volume_cover_window = settings["mdex_volume_cover_window"]
        self.use_ongoing_issue_count = settings["mdex_use_ongoing"]
        return settings

    def check_status(self, settings: dict[str, Any]) -> tuple[str, bool]:
        url = talker_utils.fix_url(settings[f"{self.id}_url"])
        if not url:
            url = self.default_api_url
        try:
            test_url = urljoin(url, "ping")
            mdex_response = requests.get(
                test_url,
                headers={"user-agent": "comictagger/" + self.version},
            )

            if mdex_response.text != "pong":
                return "Failed to contact MangaDex API", False
            return "The API access test was successful", True
        except Exception:
            return "Failed to connect to the API! Incorrect URL?", False

    def search_for_series(
        self,
        series_name: str,
        callback: Callable[[int, int], None] | None = None,
        refresh_cache: bool = False,
        literal: bool = False,
        series_match_thresh: int = 90,
    ) -> list[ComicSeries]:
        search_series_name = utils.sanitize_title(series_name, literal)
        logger.info(f"{self.name} searching: {search_series_name}")

        # Before we search online, look in our cache, since we might have done this same search recently
        # For literal searches always retrieve from online
        cvc = ComicCacher(self.cache_folder, self.version)
        if not refresh_cache and not literal:
            cached_search_results = cvc.get_search_results(self.id, series_name)
            if len(cached_search_results) > 0:
                # Unpack to apply any filters
                json_cache: list[MangaDexSeries] = [json.loads(x[0].data) for x in cached_search_results]
                if not self.adult_content:
                    json_cache = self._filter_adult(json_cache)
                if self.exclude_doujin:
                    json_cache = self._filter_dojin(json_cache)

                return self._format_search_results(json_cache)

        includes = ["cover_art", "artist", "author", "creator", "tag"]

        # Add all for cache and filter after
        content_rating = ["safe", "suggestive", "erotica", "pornographic"]

        params = {
            "title": search_series_name,
            "includes[]": includes,
            "contentRating[]": content_rating,
            "limit": 100,
            "offset": 0,
        }

        mdex_response: MangaDexResponse[list[MangaDexSeries]] = self._get_content(
            urljoin(self.api_url, "manga"), params
        )

        search_results: list[MangaDexSeries] = []

        current_result_count = len(mdex_response["data"])
        total_result_count = mdex_response["total"]

        # 1. Don't fetch more than some sane amount of pages.
        # 2. Halt when any result on the current page is less than or equal to a set ratio using thefuzz
        max_results = 500  # 5 pages

        total_result_count = min(total_result_count, max_results)

        if callback is None:
            logger.debug(f"Found {current_result_count} of {total_result_count} results")
        search_results.extend(mdex_response["data"])
        offset = 0

        if callback is not None:
            callback(len(mdex_response["data"]), total_result_count)

        # see if we need to keep asking for more pages...
        while current_result_count < total_result_count:
            if not literal:
                # Stop searching once any entry falls below the threshold
                stop_searching = any(
                    not utils.titles_match(search_series_name, series["attributes"]["title"]["en"], series_match_thresh)
                    for series in mdex_response["data"]
                )

                if stop_searching:
                    break

            if callback is None:
                logger.debug(f"getting another page of results {offset * 100} of {total_result_count}...")
            offset += 100

            params["offset"] = offset
            mdex_response = self._get_content(urljoin(self.api_url, "manga"), params)

            search_results.extend(mdex_response["data"])
            current_result_count += len(mdex_response["data"])

            if callback is not None:
                callback(current_result_count, total_result_count)

        # Cache raw data. Includes credits data as it doesn't seem to increase API time.
        cvc.add_search_results(
            self.id,
            series_name,
            [CCSeries(id=x["id"], data=json.dumps(x).encode("utf-8")) for x in search_results],
            True,
        )

        # Apply any filters
        if not self.adult_content:
            search_results = self._filter_adult(search_results)
        if self.exclude_doujin:
            search_results = self._filter_dojin(search_results)

        # Format result to ComicIssue
        formatted_search_results = self._format_search_results(search_results)

        return formatted_search_results

    def fetch_comic_data(
        self, issue_id: str | None = None, series_id: str | None = None, issue_number: str = ""
    ) -> GenericMetadata:
        comic_data = GenericMetadata()
        if issue_id:
            comic_data = self._fetch_issue_data_by_issue_id(issue_id)
        elif issue_number and series_id:
            comic_data = self._fetch_issue_data(int(series_id), issue_number)

        return comic_data

    def fetch_issues_in_series(self, series_id: str) -> list[GenericMetadata]:
        # before we search online, look in our cache, since we might already have this info
        cvc = ComicCacher(self.cache_folder, self.version)
        cached_series_issues_result = cvc.get_series_issues_info(series_id, self.id)

        series_data: MangaDexSeries = self._fetch_series(series_id)

        # A better way to check validity of cache number? Even with dedupe it is possible there is a n.5 chapter
        if len(cached_series_issues_result) > 0:
            return [
                self._map_comic_issue_to_metadata(json.loads(x[0].data), series_data)
                for x in cached_series_issues_result
            ]

        includes = ["scanlation_group"]  # Take this for publisher if "official" is True
        # Need to store all for cache and filter after
        content_rating = ["safe", "suggestive", "erotica", "pornographic"]

        offset = 0
        # TODO For now, only English data
        params = {
            "includes[]": includes,
            "contentRating[]": content_rating,
            "translatedLanguage[]": "en",
            "limit": 100,
            "offset": offset,
        }

        mdex_response: MangaDexResponse[list[MangaDexChapter]] = self._get_content(
            urljoin(self.api_url, f"manga/{series_id}/feed/"), params
        )

        current_result_count = len(mdex_response["data"])
        total_result_count = mdex_response["total"]

        series_issues_result: list[MangaDexChapter] = mdex_response["data"]

        # see if we need to keep asking for more pages...
        while current_result_count < total_result_count:
            offset += 100
            params["offset"] = offset
            mdex_response = self._get_content(urljoin(self.api_url, f"manga/{series_id}/feed/"), params)

            series_issues_result.extend(mdex_response["data"])
            current_result_count += len(mdex_response["data"])

        # Dedupe the list
        series_issues_result = self._deupe_chapters(series_issues_result)

        # Inject volume covers if required
        if self.use_volume_cover_matching or self.use_volume_cover_window:
            series_issues_result = self._volume_covers(series_id, series_issues_result)

        cvc.add_issues_info(
            self.id,
            [
                CCIssue(id=str(x["id"]), series_id=series_id, data=json.dumps(x).encode("utf-8"))
                for x in series_issues_result
            ],
            True,
        )

        formatted_series_issues_result = [
            self._map_comic_issue_to_metadata(x, series_data) for x in series_issues_result
        ]

        return formatted_series_issues_result

    def fetch_issues_by_series_issue_num_and_year(
        self, series_id_list: list[str], issue_number: str, year: str | int | None
    ) -> list[GenericMetadata]:
        # year appears unreliable with publishAt so will ignore it (related to scanlation pub date?)
        issues: list[GenericMetadata] = []

        # As this is not cached, can filter on the API. Should it be cached?
        content_rating = ["safe", "suggestive"]
        if self.adult_content:
            content_rating.append("erotica")
            content_rating.append("pornographic")

        for series_id in series_id_list:
            params = {
                "manga": series_id,
                "chapter": issue_number,
                "includes[]": ["scanlation_group"],
                "contentRating[]": content_rating,
                "translatedLanguage[]": "en",
                "limit": 100,
                "offset": 0,
            }

            mdex_response: MangaDexResponse[list[MangaDexChapter]] = self._get_content(
                urljoin(self.api_url, "chapter"), params
            )
            series = self._fetch_series(series_id)

            current_result_count = len(mdex_response["data"])
            total_result_count = mdex_response["total"]

            issues_result: list[MangaDexChapter] = mdex_response["data"]

            offset = 0

            # see if we need to keep asking for more pages...
            while current_result_count < total_result_count:
                offset += 100

                params["offset"] = offset
                mdex_response = self._get_content(urljoin(self.api_url, "chapter"), params)

                current_result_count += len(mdex_response["data"])
                issues_result.extend(mdex_response["data"])

            issues_result = self._deupe_chapters(issues_result)

            # Inject volume covers if required
            if self.use_volume_cover_matching or self.use_volume_cover_window:
                issues_result = self._volume_covers(series_id, issues_result)

            for issue in issues_result:
                issues.append(self._map_comic_issue_to_metadata(issue, series))

        return issues

    @limiter.ratelimit("default", delay=True)
    def _get_content(self, url: str, params: dict[str, Any]) -> MangaDexResponse:
        mdex_response: MangaDexResponse = self._get_url_content(url, params)
        if mdex_response.get("result") == "error":
            logger.debug(f"{self.name} query failed with error: {mdex_response['errors']}")
            raise TalkerNetworkError(self.name, 0, f"{mdex_response['errors']}")

        return mdex_response

    def _get_url_content(self, url: str, params: dict[str, Any]) -> Any:
        for tries in range(3):
            try:
                resp = requests.get(url, params=params, headers={"user-agent": "comictagger/" + self.version})

                if resp.status_code == requests.status_codes.codes.ok:
                    return resp.json()
                if resp.status_code == requests.status_codes.codes.server_error:
                    logger.debug(f"Try #{tries + 1}: ")
                    time.sleep(1)
                    logger.debug(str(resp.status_code))
                if resp.status_code == requests.status_codes.codes.bad_request:
                    logger.debug(f"Bad request: {resp.json()}")
                    raise TalkerNetworkError(self.name, 2, f"Bad request: {resp.json()}")
                if resp.status_code == requests.status_codes.codes.forbidden:
                    logger.debug(f"Forbidden: {resp.json()}")
                    raise TalkerNetworkError(self.name, 2, f"Forbidden: {resp.json()}")
                if resp.status_code == requests.status_codes.codes.not_found:
                    logger.debug(f"Manga not found: {resp.json()}")
                    raise TalkerNetworkError(self.name, 2, f"Manga not found: {resp.json()}")
                # Should never get here but in case something is changed with limits
                if resp.status_code == requests.status_codes.codes.too_many_requests:
                    logger.debug(f"Rate limit reached: {resp.json()}")
                    # If given a time to wait before re-trying, use that time + 1
                    if resp.headers.get("x-ratelimit-retry-after", None):
                        wait_time = int(resp.headers["x-ratelimit-retry-after"]) - int(time.time())
                        if wait_time > 0:
                            time.sleep(wait_time + 1)
                    else:
                        time.sleep(10)
                else:
                    break

            except requests.exceptions.Timeout:
                logger.debug(f"Connection to {self.name} timed out.")
                raise TalkerNetworkError(self.name, 4)
            except requests.exceptions.RequestException as e:
                logger.debug(f"Request exception: {e}")
                raise TalkerNetworkError(self.name, 0, str(e)) from e
            except json.JSONDecodeError as e:
                logger.debug(f"JSON decode error: {e}")
                raise TalkerDataError(self.name, 2, f"{self.name} did not provide json")

        raise TalkerNetworkError(self.name, 5)

    # Search results and full series data
    def _format_search_results(self, search_results: list[MangaDexSeries]) -> list[ComicSeries]:
        formatted_results = []
        for record in search_results:
            alias_list = set()
            for alias in record["attributes"]["altTitles"]:
                for iso, title in alias.items():
                    alias_list.add(title)

            # TODO Use language preference?
            # "en" is not guaranteed
            title = next(iter(record["attributes"]["title"].values()))

            # Publisher can only be gleaned from chapter information
            pub_name = ""

            start_year = utils.xlate_int(record["attributes"].get("year"))

            format_type = ""
            for mdex_tags in record["attributes"]["tags"]:
                if mdex_tags["attributes"]["group"] == "format":
                    format_type = mdex_tags["attributes"]["name"]["en"]

            # TODO Use local language or setting etc.?
            desc = record["attributes"]["description"].get("en", "")

            image_url = ""

            # Parse relationships
            for rel in record["relationships"]:
                if rel["type"] == "cover_art":
                    image_url = urljoin(self.cover_url_base, f"{record['id']}/{rel['attributes']['fileName']}")

            formatted_results.append(
                ComicSeries(
                    aliases=alias_list,
                    count_of_issues=utils.xlate_int(record["attributes"].get("lastChapter", None)),
                    count_of_volumes=utils.xlate_int(record["attributes"].get("lastVolume", None)),
                    description=desc,
                    id=str(record["id"]),
                    image_url=image_url,
                    name=title,
                    publisher=pub_name,
                    start_year=start_year,
                    format=format_type,
                )
            )

        return formatted_results

    def _deupe_chapters(self, chapters: list[MangaDexChapter]) -> list[MangaDexChapter]:
        # Because a chapter may have multiple release groups, dedupe with preference for "official" publisher
        unique_chapters = {}

        for i, chapter in enumerate(chapters):
            chapter_number = chapter["attributes"]["chapter"]
            is_official = False
            for rel in chapter["relationships"]:
                if rel.get("attributes") and rel["attributes"]["official"]:
                    is_official = True

            # Check if the chapter number is already in the dictionary and replace with official if so
            if chapter_number in unique_chapters and is_official:
                unique_chapters[chapter_number] = i
            else:
                unique_chapters[chapter_number] = i

        dedupe_chapters = [chapter for i, chapter in enumerate(chapters) if i in unique_chapters.values()]

        return dedupe_chapters

    def _filter_adult(self, series_results: list[MangaDexSeries]) -> list[MangaDexSeries]:
        def is_adult(series):
            content_rating = series["attributes"]["contentRating"]
            tags = series["attributes"]["tags"]

            return content_rating in ["erotica", "pornographic"] or any(
                tag["attributes"]["group"] == "content" for tag in tags
            )

        return [series for series in series_results if not is_adult(series)]

    def _filter_dojin(self, series_results: list[MangaDexSeries]) -> list[MangaDexSeries]:
        return [
            series
            for series in series_results
            if not any(
                tag["attributes"]["group"] in ["genre", "format"] and tag["attributes"]["name"]["en"] == "Doujinshi"
                for tag in series["attributes"]["tags"]
            )
        ]

    def fetch_series(self, series_id: str) -> ComicSeries:
        return self._format_search_results([self._fetch_series(series_id)])[0]

    def _fetch_series(self, series_id: str) -> MangaDexSeries:
        # Search returns the full series information, this is a just in case
        cvc = ComicCacher(self.cache_folder, self.version)
        cached_series_result = cvc.get_series_info(series_id, self.id)
        if cached_series_result is not None:
            return json.loads(cached_series_result[0].data)

        # Include information for credits for use when tagging an issue
        params = {"includes[]": ["cover_art", "author", "artist", "tag", "creator"]}
        series_url = urljoin(self.api_url, f"manga/{series_id}")
        mdex_response: MangaDexResponse[MangaDexSeries] = self._get_content(series_url, params)

        if mdex_response:
            cvc.add_series_info(
                self.id,
                CCSeries(id=str(mdex_response["data"]["id"]), data=json.dumps(mdex_response["data"]).encode("utf-8")),
                True,
            )

        return mdex_response["data"]

    def _fetch_issue_data(self, series_id: int, issue_number: str) -> GenericMetadata:
        # Should be in cache but will cover if not
        # issue number presumed to be chapter number
        params = {"manga": series_id, "chapter": issue_number}

        issue_url = urljoin(self.api_url, "chapter")
        mdex_response: MangaDexResponse[MangaDexChapter] = self._get_content(issue_url, params)

        if mdex_response["data"]["id"]:
            return self._fetch_issue_data_by_issue_id(mdex_response["data"]["id"])
        return GenericMetadata()

    def _fetch_issue_data_by_issue_id(self, issue_id: str) -> GenericMetadata:
        # All data should be cached but will cover anyway
        # issue number presumed to be chapter number
        series_id = ""

        cvc = ComicCacher(self.cache_folder, self.version)
        cached_issues_result = cvc.get_issue_info(issue_id, self.id)

        if cached_issues_result and cached_issues_result[1]:
            return self._map_comic_issue_to_metadata(
                json.loads(cached_issues_result[0].data), self._fetch_series(cached_issues_result[0].series_id)
            )

        # scanlation group wanted to try and glean publisher if "official" is True
        params = {"includes[]": ["scanlation_group"]}

        issue_url = urljoin(self.api_url, f"chapter/{issue_id}")
        mdex_response: MangaDexResponse[MangaDexChapter] = self._get_content(issue_url, params)

        # Find series_id
        for rel in mdex_response["data"]["relationships"]:
            if rel["type"] == "manga":
                series_id = rel["id"]

        issue_result = mdex_response["data"]
        series_result: MangaDexSeries = self._fetch_series(series_id)

        cvc.add_issues_info(
            self.id,
            [
                CCIssue(
                    id=str(issue_result["id"]),
                    series_id=series_id,
                    data=json.dumps(issue_result).encode("utf-8"),
                )
            ],
            True,
        )

        return self._map_comic_issue_to_metadata(issue_result, series_result)

    def _volume_covers(self, series_id: str, issues: list[MangaDexChapter]) -> list[MangaDexChapter]:
        # As chapters do not have covers, fetch the volume cover the chapter is contained within
        cover_url = urljoin(self.api_url, "cover")
        offset = 0
        params = {
            "manga[]": series_id,
            "limit": 100,
            "offset": offset,
        }

        covers_for_series: MangaDexResponse[list[MangaDexCover]] = self._get_content(cover_url, params)

        current_result_count = len(covers_for_series["data"])
        total_result_count = covers_for_series["total"]

        # see if we need to keep asking for more pages...
        while current_result_count < total_result_count:
            offset += 100
            params["offset"] = offset
            mdex_response = self._get_content(cover_url, params)

            covers_for_series["data"].extend(mdex_response["data"])
            current_result_count += len(mdex_response["data"])

        # Match chapter to volume cover
        for issue in issues:
            if issue["attributes"].get("volume"):
                wanted_volume = issue["attributes"]["volume"]
                for cover in covers_for_series["data"]:
                    if cover["attributes"]["volume"] == wanted_volume:
                        issue["attributes"]["image"] = urljoin(
                            self.cover_url_base, f"{series_id}/{cover['attributes']['fileName']}"
                        )
                        break

        return issues

    def _map_comic_issue_to_metadata(self, issue: MangaDexChapter, series: MangaDexSeries) -> GenericMetadata:
        md = GenericMetadata(
            data_origin=MetadataOrigin(self.id, self.name),
            issue_id=utils.xlate(issue["id"]),
            series_id=utils.xlate(series["id"]),
            issue=utils.xlate(IssueString(issue["attributes"]["chapter"]).as_string()),
        )
        # TODO Language support?
        md.series = utils.xlate(series["attributes"]["title"]["en"])

        md.manga = "Yes"

        md.cover_image = issue["attributes"].get("image")

        # Check if series is ongoing to legitimise issue count OR use option setting
        # Having a lastChapter indicated completed or cancelled
        if series["attributes"]["lastChapter"] or self.use_ongoing_issue_count:
            md.issue_count = utils.xlate_int(series["attributes"]["lastChapter"])
            md.volume_count = utils.xlate_int(series["attributes"]["lastVolume"])

        # TODO Select language?
        # TODO Option to copy series desc or not?
        if series["attributes"].get("description"):
            md.description = next(iter(series["attributes"]["description"].values()), None)

        if series["attributes"].get("tags"):
            # Tags holds genre, theme, content warning and format
            genres = []
            tags = []
            format_type = None

            for mdex_tags in series["attributes"]["tags"]:
                if mdex_tags["attributes"]["group"] == "genre":
                    genres.append(mdex_tags["attributes"]["name"]["en"])

                if mdex_tags["attributes"]["group"] == "format":
                    if mdex_tags["attributes"]["name"]["en"] in ["Web Comic", "Oneshot"]:
                        format_type = mdex_tags["attributes"]["name"]["en"]
                    else:
                        tags.append(mdex_tags["attributes"]["name"]["en"])

                if mdex_tags["attributes"]["group"] in ["theme", "content"]:
                    tags.append(mdex_tags["attributes"]["name"]["en"])

            md.genres = set(genres)
            md.tags = set(tags)
            md.format = format_type

        md.title = utils.xlate(issue["attributes"]["title"])

        for alt_title in series["attributes"].get("altTitles", set()):
            md.series_aliases.add(next(iter(alt_title.values())))

        md.language = utils.xlate(issue["attributes"].get("translatedLanguage"))

        if series["attributes"].get("contentRating"):
            md.maturity_rating = series["attributes"]["contentRating"].capitalize()

        # Can't point to an "issue" per se, so point to series
        md.web_link = urljoin(self.website, f"title/{series['id']}")

        # Parse relationships
        for rel in series["relationships"]:
            if rel["type"] == "author":
                md.add_credit(rel["attributes"]["name"], "writer")
            if rel["type"] == "artist":
                md.add_credit(rel["attributes"]["name"], "artist")

            if rel["type"] == "scanlation_group" and rel["attributes"]["official"]:
                md.publisher = rel["attributes"]["name"]

        md.volume = utils.xlate_int(issue["attributes"]["volume"])

        if self.use_series_start_as_volume:
            md.volume = utils.xlate_int(series["attributes"]["year"])

        if issue["attributes"].get("publishAt"):
            publish_date = datetime.datetime.fromisoformat(issue["attributes"]["publishAt"])
            md.day, md.month, md.year = utils.parse_date_str(publish_date.strftime("%Y-%m-%d"))
        elif series["attributes"].get("year"):
            md.year = utils.xlate_int(series["attributes"].get("year"))

        return md
