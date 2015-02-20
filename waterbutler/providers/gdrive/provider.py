import os
import asyncio
import json
from urllib.parse import urlparse

from waterbutler.core import utils
from waterbutler.core import streams
from waterbutler.core import provider
from waterbutler.core import exceptions

from waterbutler.providers.gdrive import settings
from waterbutler.providers.gdrive.metadata import GoogleDriveRevision
from waterbutler.providers.gdrive.metadata import GoogleDriveFileMetadata
from waterbutler.providers.gdrive.metadata import GoogleDriveFolderMetadata


class GoogleDrivePath(utils.WaterButlerPath):

    def __init__(self, path, folder, prefix=True, suffix=False):
        super().__init__(path, prefix=prefix, suffix=suffix)

        self.path_parts = path.strip('/').split('/')
        self.folder = folder
        self.upload_file_name = ''

        if len(self.path_parts) > 1:
            self.folder_id = self.path_parts[0]
            self.folder_name = self.path_parts[1]

    # If a slash can be included before file.name while building uploadUrl
    # in waterbutler.js then this part won't be necessary
    def upload_path(self):
        folder_plus_name = self.path_parts[-1:][0]
        folder_name = self.folder['path']['path'].split('/')[-1:][0]
        start_index = folder_plus_name.find(folder_name)
        upload_file_name = folder_plus_name[start_index + len(folder_name):]
        self.upload_file_name = upload_file_name
        upload_path =os.path.join(self.full_path.rstrip(upload_file_name), upload_file_name)
        return upload_path

    @property
    def full_path(self):
        path = ''
        for i in range(2, len(self.path_parts)):
            if path == '':
                path = self.path_parts[i]
            else:
                path = path + '/' + self.path_parts[i]
        return path

    def __repr__(self):
        return "{}({!r}, {!r})".format(self.__class__.__name__, self.folder_id, self._orig_path)


class GoogleDriveProvider(provider.BaseProvider):

    BASE_URL = settings.BASE_URL
    BASE_UPLOAD_URL = settings.BASE_UPLOAD_URL

    def __init__(self, auth, credentials, settings):
        super().__init__(auth, credentials, settings)
        self.token = self.credentials['token']
        # self.refresh_token = self.credentials['refresh_token']
        # self.client_id = self.credentials['client_id']
        # self.client_secret = self.credentials['client_secret']
        self.folder = self.settings['folder']

    @property
    def default_headers(self):
        return {'authorization': 'Bearer {}'.format(self.token)}

    @asyncio.coroutine
    def download(self, path, revision=None, **kwargs):

        path = GoogleDrivePath(path, self.folder)
        resp = yield from self.make_request(
            'GET',
            self.build_url('files', path.folder_id),
            expects=(200, ),
            throws=exceptions.DownloadError,
        )
        data = yield from resp.json()
        download_url = data['downloadUrl']
        download_resp = yield from self.make_request(
            'GET',
            download_url,
            expects=(200, ),
            throws=exceptions.DownloadError,
        )

        return streams.ResponseStreamReader(download_resp)

    @asyncio.coroutine
    def upload(self, stream, path, **kwargs):
        path = GoogleDrivePath(path, self.folder)
        path_for_metadata = os.path.join('/', path.folder_id, path.upload_file_name, path.upload_path())
        try:
            yield from self.metadata(str(path_for_metadata))
        except exceptions.MetadataError:
            print("in exception")
            created = True
        else:
            created = False
        content = yield from stream.read()
        metadata = {
            "parents": [
                {
                    "kind": "drive#parentReference",
                    "id": path.folder_id
                },
            ],
            "title": path.upload_file_name,
        }
        #Step 1 - Start a resumable session
        resp = yield from self.make_request(
            'POST',
            self._build_upload_url("files", uploadType='resumable'),
            headers={'Content-Length': str(len(json.dumps(metadata))),
                     'Content-Type': 'application/json; charset=UTF-8',
                     #'X-Upload-Content-Type': 'image/jpeg',#hardcoded in for testing
                     'X-Upload-Content-Length': str(stream.size)
                     },
            data=json.dumps(metadata),
            expects=(200, ),
            throws=exceptions.UploadError
        )
        #Step 2 - Save the resumable session URI
        yield from resp.json()
        location = resp.headers['LOCATION']
        query_params = urlparse(location).query
        #todo:make this proper
        upload_id = query_params.split("=")[2]
        #Step 3 - Upload the file
        resp = yield from self.make_request(
            'PUT',
            self._build_upload_url("files", uploadType='resumable', upload_id=upload_id),
            headers={'Content-Length': str(stream.size),
                     #'Content-Type': 'image/jpeg',#todo: hardcoded in for testing
                     },
            data=content,
            expects=(200, ),
            throws=exceptions.UploadError,
        )
        data = yield from resp.json()
        data['path'] = path.upload_path()
        return GoogleDriveFileMetadata(data, self.folder).serialized(), created

    @asyncio.coroutine
    def delete(self, path, **kwargs):
        # A metadata call will verify the path specified is actually the
        # requested file or folder.
        yield from self.metadata(str(path))
        path = GoogleDrivePath(path, self.folder)
        yield from self.make_request(
            'DELETE',
            self.build_url('files', path.folder_id),
            expects=(204, ),
            throws=exceptions.DeleteError,
        )

    @asyncio.coroutine
    def metadata(self, path, **kwargs):
        # import pdb; pdb.set_trace()
        path = GoogleDrivePath(path, self.folder)

        resp = yield from self.make_request(
            'GET',
            self.build_url('files', q="'%s' in parents and trashed = false" % path.folder_id, alt="json"),
            expects=(200, ),
            throws=exceptions.MetadataError
        )
        data = yield from resp.json()

        # Check to see if request was made for file or folder
        if len(data['items']) == 0:  # No subitems indicates a File
            resp = yield from self.make_request(
                'GET',
                self.build_url('files', path.folder_id),
                expects=(200, ),
                throws=exceptions.MetadataError
            )
            data = yield from resp.json()
            data['path'] = os.path.join(path.full_path, data['title'])

        else:  # Folder
            ret = []
            for item in data['items']:
                item['path'] = os.path.join(path.full_path, item['title'])  # custom add, not obtained from API
                if item['mimeType'] == 'application/vnd.google-apps.folder':
                    ret.append(GoogleDriveFolderMetadata(item, self.folder).serialized())
                else:
                    ret.append(GoogleDriveFileMetadata(item, self.folder).serialized())
            return ret

        return GoogleDriveFileMetadata(data, self.folder).serialized()

    @asyncio.coroutine
    def revisions(self, path, **kwargs):
        path = GoogleDrivePath(path, self.folder)
        response = yield from self.make_request(
            'GET',
            self.build_url('files', path.folder_id, 'revisions'),
            expects=(200, ),
            throws=exceptions.RevisionsError
        )
        data = yield from response.json()
        return [
            GoogleDriveRevision(item).serialized()
            for item in data['items']
        ]

    def _build_upload_url(self, *segments, **query):
        return provider.build_url(settings.BASE_UPLOAD_URL, *segments, **query)
