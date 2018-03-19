'''Active Programming Interface (API).'''
import re
import os
import logging
import email
import six
from io import BytesIO
from collections import OrderedDict

from PIL import Image
import requests
import pydicom

from dicomweb_client.error import DICOMJSONError


logger = logging.getLogger(__name__)


def _init_dataset():
    '''Creates an empty DICOM Data Set.

    Returns
    -------
    pydicom.dataset.Dataset

    '''
    return pydicom.dataset.Dataset()


def _create_dataelement(tag, vr, value):
    '''Creates a DICOM Data Element.

    Parameters
    ----------
    tag: pydicom.tag.Tag
        data element tag
    vr: str
        data element value representation
    value: list
        data element value(s)

    Returns
    -------
    pydicom.dataelem.DataElement

    '''
    binary_representations = {'OB', 'OD', 'OF', 'OL', 'OW', 'UN'}
    try:
        vm = pydicom.datadict.dictionary_VM(tag)
    except KeyError:
        # Private tag
        vm = str(len(value))
    if vr not in binary_representations:
        if not(isinstance(value, list)):
            raise DICOMJSONError(
                '"Value" of data element "{}" must be an array.'.format(tag)
                )
    if vr == 'SQ':
        value = value[0]
        ds = _init_dataset()
        if value is not None:
            for key, val in value.items():
                if 'vr' not in val:
                    raise DICOMJSONError(
                        'Data element "{}" must have key "vr".'.format(tag)
                    )
                supported_value_keys = {'Value', 'BulkDataURI', 'InlineBinary'}
                val_key = None
                for k in supported_value_keys:
                    if k in val:
                        val_key = k
                        break
                if val_key is None:
                    logger.warn(
                        'data element has neither key "{}".'.format(
                            '" nor "'.join(supported_value_keys)
                        )
                    )
                    e = pydicom.dataelem.DataElement(tag=tag, value=None, VR=vr)
                else:
                    e = _create_dataelement(key, val['vr'], val[val_key])
                ds.add(e)
        elem_value = [ds]
    elif vr == 'PN':
        # Special case, see DICOM Part 18 Annex F2.2
        # http://dicom.nema.org/medical/dicom/current/output/html/part18.html#sect_F.2.2
        elem_value = []
        for v in value:
            if not isinstance(v, dict):
                # Some DICOMweb services get this wrong, so we workaround it
                # and issue a warning rather than raising an error.
                logger.warn(
                    'attribute with VR Person Name (PN) is not '
                    'formatted correctly'
                )
                elem_value.append(v)
            else:
                # TODO: How to handle "Ideographic" and "Phonetic"?
                elem_value.append(v['Alphabetic'])
    else:
        if vm == '1':
            if vr in binary_representations:
                elem_value = value
            else:
                if value:
                    elem_value = value[0]
                else:
                    elem_value = value
        else:
            if len(value) == 1 and isinstance(value[0], six.string_types):
                elem_value = value[0].split('\\')
            else:
                elem_value = value
    if value is None:
        # TODO: check if mandatory
        logger.warn('missing value for data element "{}"'.format(tag))
    return pydicom.dataelem.DataElement(tag=tag, value=elem_value, VR=vr)


def _map_color_mode(photometic_interpretation):
    '''Maps a DICOM *photometric interpretation* to Python Pillow color *mode*.

    Parameters
    ----------
    photometic_interpretation: str
        photometric interpretation

    Returns
    -------
    str
        color mode

    '''
    color_map = {
        'RGB': 'RGB',
        'MONOCHROME2': 'L',
        'YBR_FULL_422': 'YCbCr'
    }
    try:
        mode = color_map[photometic_interpretation]
    except IndexError:
        raise ValueError(
            'Photometric interpretation "{}" is not supported.'.format(
                photometic_interpretation
            )
        )
    return mode


def load_json_dataset(dataset):
    '''Loads DICOM Data Set in DICOM JSON format.

    Parameters
    ----------
    dataset: Dict[str, dict]
        mapping where keys are DICOM *Tags* and values are mappings of DICOM
        *VR* and *Value* key-value pairs

    Returns
    -------
    pydicom.dataset.Dataset

    '''
    ds = _init_dataset()
    for tag, mapping in dataset.items():
        vr = mapping['vr']
        try:
            value = mapping['Value']
        except KeyError:
            logger.warn(
                'mapping for data element "{}" has no "Value" key'.format(tag)
            )
            value = [None]
        de = _create_dataelement(tag, vr, value)
        ds.add(de)
    return ds


class DICOMWebClient(object):

    '''Class for connecting to and interacting with a DICOMweb RESTful service.

    Attributes
    ----------
    base_url: str
        unique resource locator of the DICOMweb service
    protocol: str
        name of the protocol, e.g. ``"https"``
    host: str
        IP address or DNS name of the machine that hosts the server
    port: int
        number of the port to which the server listens
    prefix: str
        URL prefix

    '''

    def __init__(self, url, username=None, password=None, ca_bundle=None):
        '''
        Parameters
        ----------
        url: str
            base unique resource locator consisting of protocol, hostname
            (IP address or DNS name) of the machine that hosts the server and
            optionally port number and path prefix
        username: str
            username for authentication with the server
        password: str
            password for authentication with the server
        ca_bundle: str, optional
            path to CA bundle file in Privacy Enhanced Mail (PEM) format

        '''
        logger.debug('initialize HTTP session')
        self._session = requests.Session()
        self.base_url = url
        if self.base_url.startswith('https'):
            if ca_bundle is not None:
                ca_bundle = os.path.expanduser(os.path.expandvars(ca_bundle))
                if not os.path.exists(ca_bundle):
                    raise OSError(
                        'CA bundle file does not exist: {}'.format(ca_bundle)
                    )
                logger.debug('use CA bundle file: {}'.format(ca_bundle))
                self._session.verify = ca_bundle
        # This regular expressin extracts the scheme and host name from the URL
        # and optionally the port number and prefix:
        # <scheme>://<host>(:<port>)(/<prefix>)
        # For example: "https://mydomain.com:80/wado-rs", where
        # scheme="https", host="mydomain.com", port=80, prefix="wado-rs"
        pattern = re.compile(
            '(?P<scheme>[https]+)://(?P<host>[^/:]+)'
            '(?::(?P<port>\d+))?(?:(?P<prefix>/\w+))?'
        )
        match = re.match(pattern, self.base_url)
        self.protocol = match.group('scheme')
        self.host = match.group('host')
        self.port = match.group('port')
        if self.port is not None:
            self.port = int(self.port)
        else:
            if self.protocol == 'http':
                self.port = 80
            elif self.protocol == 'https':
                self.port = 443
            else:
                raise ValueError(
                    'URL scheme "{}" is not supported.'.format(self.protocol)
                )
        self.prefix = match.group('prefix')
        if self.prefix is None:
            self.prefix = ''
        self._session.headers.update({'Host': self.host})
        if username is not None:
            if password is None:
                logger.warn('no password provided')
            self._session.auth = (username, password)

    def _parse_query_parameters(self, fuzzymatching, limit, offset,
                                fields, **search_filters):
        params = dict()
        if limit is not None:
            if not(isinstance(limit, int)):
                raise TypeError('Parameter "limit" must be an integer.')
            if limit < 0:
                raise ValueError('Parameter "limit" must not be negative.')
            params['limit'] = limit
        if offset is not None:
            if not(isinstance(offset, int)):
                raise TypeError('Parameter "offset" must be an integer.')
            if offset < 0:
                raise ValueError('Parameter "offset" must not be negative.')
            params['offset'] = offset
        if fuzzymatching is not None:
            if not(isinstance(fuzzymatching, bool)):
                raise TypeError('Parameter "fuzzymatching" must be boolean.')
            if fuzzymatching:
                params['fuzzymatching'] = 'true'
            else:
                params['fuzzymatching'] = 'false'
        if fields is not None:
            for field in set(fields):
                if not(isinstance(field, str)):
                    raise TypeError('Elements of "fields" must be a string.')
                params['includefield'] = field
        for field, criterion in search_filters.items():
            if not(isinstance(field, str)):
                raise TypeError('Keys of "search_filters" must be strings.')
            # TODO: datetime?
            params[field] = criterion
        # Sort query parameters to facilitate unit testing
        return OrderedDict(sorted(params.items()))

    def _get_studies_url(self, study_instance_uid=None):
        if study_instance_uid is not None:
            url = '{service}/studies/{study_instance_uid}'
        else:
            url = '{service}/studies'
        return url.format(
            service=self.base_url, study_instance_uid=study_instance_uid
        )

    def _get_series_url(self, study_instance_uid=None,
                        series_instance_uid=None):
        if study_instance_uid is not None:
            url = self._get_studies_url(study_instance_uid)
            if series_instance_uid is not None:
                url += '/series/{series_instance_uid}'
            else:
                url += '/series'
        else:
            if series_instance_uid is not None:
                logger.warn(
                    'series UID is ignored because study UID is undefined'
                )
            url = '{service}/series'
        return url.format(
            service=self.base_url, series_instance_uid=series_instance_uid
        )

    def _get_instances_url(self, study_instance_uid=None,
                           series_instance_uid=None, sop_instance_uid=None):
        if study_instance_uid is not None and series_instance_uid is not None:
            url = self._get_series_url(study_instance_uid, series_instance_uid)
            url += '/instances'
            if sop_instance_uid is not None:
                url += '/{sop_instance_uid}'
        else:
            if sop_instance_uid is not None:
                logger.warn(
                    'instance UID is ignored because study and/or instance '
                    'UID are undefined'
                )
            url = '{service}/instances'
        return url.format(
            service=self.base_url, sop_instance_uid=sop_instance_uid
        )

    def _http_get(self, url, params, headers):
        '''Performs a HTTP GET request.

        Parameters
        ----------
        url: str
            unique resource locator
        params: Dict[str]
            query parameters
        headers: Dict[str]
            HTTP request message headers

        Returns
        -------
        requests.models.Response
            HTTP response message

        '''
        logger.debug('GET: {}'.format(url))
        resp = self._session.get(url=url, params=params, headers=headers)
        resp.raise_for_status()
        return resp

    def _http_get_application_json(self, url, **params):
        '''Performs a HTTP GET request that accepts "applicaton/dicom+json"
        media type.

        Parameters
        ----------
        url: str
            unique resource locator
        params: Dict[str]
            query parameters

        Returns
        -------
        Union[dict, list, types.NoneType]
            content of HTTP message body in DICOM JSON format

        '''
        content_type = 'application/dicom+json'
        resp = self._http_get(url, params, {'Accept': content_type})
        if resp.content:
            return resp.json()

    def _http_get_application_dicom(self, url, **params):
        '''Performs a HTTP GET request that accepts "applicaton/dicom"
        media type.

        Parameters
        ----------
        url: str
            unique resource locator
        params: Dict[str]
            query parameters

        Returns
        -------
        pydicom.dataset.Dataset
            DICOM data set

        '''
        content_type = 'application/dicom'
        resp = self._http_get(url, params, {'Accept': content_type})
        return pydicom.dcmread(BytesIO(resp.content))

    @staticmethod
    def _decode_multipart_message(body, headers):
        '''Extracts parts of a HTTP multipart response message.

        Parameters
        ----------
        body: Union[str, bytes]
            HTTP response message body
        headers: Dict[str, str]
            HTTP response message headers

        Returns
        -------
        List[Union[str, bytes]]
            data

        '''
        header = ''
        for key, value in headers.items():
            header += '{}: {}\n'.format(key, value)
        if isinstance(body, str):
            message = email.message_from_string(header + body)
        else:
            message = email.message_from_bytes(header.encode() + body)
        elements = list()
        for part in message.walk():
            if part.get_content_maintype() == 'multipart':
                # NOTE for http://wg26.pathcore.com/wado
                # If only one frame number is provided, returns a normal
                # message body instead of a multipart message body.
                if part.is_multipart():
                    continue
            payload = part.get_payload(decode=True)
            elements.append(payload)
        return elements

    @staticmethod
    def _encode_multipart_message(data, content_type):
        '''Extracts parts of a HTTP multipart response message.

        Parameters
        ----------
        data: List[Union[str, bytes]]
            data
        content_type: str
            content type of the multipart HTTP request message

        Returns
        -------
        Union[str, bytes]
            HTTP request message body

        '''
        multipart, content_type, boundary = content_type.split(';')
        boundary = boundary[boundary.find('=')+2:-1]
        content_type = content_type[content_type.find('=')+2:-1]
        body = b''
        for payload in data:
            body += (
                '\r\n--{boundary}'
                '\r\nContent-Type: {content_type}\r\n\r\n'.format(
                    boundary=boundary, content_type=content_type
                ).encode('utf-8')
            )
            body += payload
        body += '\r\n--{boundary}--'.format(boundary=boundary).encode('utf-8')
        return body

    def _http_get_multipart_application_dicom(self, url, **params):
        '''Performs a HTTP GET request that accepts a multipart message with
        "applicaton/dicom" media type.

        Parameters
        ----------
        url: str
            unique resource locator
        params: Dict[str]
            query parameters

        Returns
        -------
        List[pydicom.dataset.Dataset]
            DICOM data sets

        '''
        content_type = 'multipart/related; type="application/dicom"'
        resp = self._http_get(url, params, {'Accept': content_type})
        datasets = self._decode_multipart_message(resp.content, resp.headers)
        return [pydicom.dcmread(BytesIO(ds)) for ds in datasets]

    def _http_get_multipart_application_octet_stream(self, url, **params):
        '''Performs a HTTP GET request that accepts a multipart message with
        "applicaton/octet-stream" media type.

        Parameters
        ----------
        url: str
            unique resource locator
        params: Dict[str]
            query parameters

        Returns
        -------
        List[Union[str, bytes]]
            content of HTTP message body parts

        '''
        content_type = 'multipart/related; type="application/octet-stream"'
        resp = self._http_get(url, params, {'Accept': content_type})
        return self._decode_multipart_message(resp.content, resp.headers)

    def _http_get_multipart_image(self, url, compression, **params):
        '''Performs a HTTP GET request that accepts a multipart message with
        "image/{compression}" media type.

        Parameters
        ----------
        url: str
            unique resource locator
        params: Dict[str]
            query parameters

        Returns
        -------
        List[Union[str, bytes]]
            content of HTTP message body parts

        '''
        content_type = 'multipart/related; type="image/{compression}"'.format(
            compression=compression
        )
        resp = self._http_get(url, params, {'Accept': content_type})
        return self._decode_multipart_message(resp.content, resp.headers)

    def _http_post(self, url, data, headers):
        '''Performs a HTTP POST request.

        Parameters
        ----------
        url: str
            unique resource locator
        data: Union[str, bytes]
            HTTP request message payload
        headers: Dict[str]
            HTTP request message headers

        Returns
        -------
        requests.models.Response
            HTTP response message

        '''
        logger.debug('POST: {}'.format(url))
        response = self._session.post(url=url, data=data, headers=headers)
        response.raise_for_status()
        return response

    def _http_post_multipart_application_dicom(self, url, datasets):
        '''Performs a HTTP POST request

        Parameters
        ----------
        url: str
            unique resource locator
        datasets: List[pydicom.dataset.Dataset]
            DICOM data sets that should be posted

        '''
        content_type = (
            'multipart/related; '
            'type="application/dicom"; '
            'boundary="boundary"'
        )
        encoded_datasets = list()
        # TODO: can we do this more memory efficient?
        for ds in datasets:
            with BytesIO() as b:
                pydicom.dcmwrite(b, ds)
                encoded_datasets.append(b.getvalue())
        data = self._encode_multipart_message(encoded_datasets, content_type)
        self._http_post(url, data, headers={'Content-Type': content_type})

    def search_studies(self, fuzzymatching=None, limit=None, offset=None,
                       fields=None, search_filters={}):
        '''Searches DICOM studies.

        Parameters
        ----------
        fuzzymatching: bool, optional
            whether fuzzy semantic matching should be performed
        limit: int, optional
            maximum number of results that should be returned
        offset: int, optional
            number of results that should be skipped
        fields: List[str], optional
            names of fields (attributes) that should be included in results
        search_filters: dict, optional
            search filter criteria as key-value pairs, where *key* is a keyword
            or a tag of the attribute and *value* is the expected value that
            should match

        Returns
        -------
        List[Dict[str, dict]]
            matching instances

        '''
        url = self._get_studies_url()
        params = self._parse_query_parameters(
            fuzzymatching, limit, offset, fields, **search_filters
        )
        studies = self._http_get_application_json(url, **params)
        if studies is None:
            return []
        if not(isinstance(studies, list)):
            studies = [studies]
        return studies

    def retrieve_study(self, study_instance_uid):
        '''Retrieves instances of a given DICOM study.

        Parameters
        ----------
        study_instance_uid: str
            unique study identifier

        Returns
        -------
        List[pydicom.dataset.Dataset]
            data sets

        '''
        if study_instance_uid is None:
            raise ValueError('Study UID is required for retrieval of study.')
        url = self._get_studies_url(study_instance_uid)
        return self._http_get_multipart_application_dicom(url)

    def retrieve_study_metadata(self, study_instance_uid):
        '''Retrieves metadata of instances of a given DICOM study.

        Parameters
        ----------
        study_instance_uid: str
            unique study identifier

        Returns
        -------
        Dict[str, dict]
            metadata in DICOM JSON format

        '''
        if study_instance_uid is None:
            raise ValueError(
                'Study UID is required for retrieval of study metadata.'
            )
        url = self._get_studies_url(study_instance_uid)
        url += '/metadata'
        return self._http_get_application_json(url)

    def search_series(self, study_instance_uid=None, fuzzymatching=None,
                      limit=None, offset=None, fields=None, search_filters={}):
        '''Searches DICOM series.

        Parameters
        ----------
        study_instance_uid: str, optional
            unique study identifier
        fuzzymatching: bool, optional
            whether fuzzy semantic matching should be performed
        limit: int, optional
            maximum number of results that should be returned
        offset: int, optional
            number of results that should be skipped
        fields: Union[list, tuple, set], optional
            names of fields (attributes) that should be included in results
        search_filters: Dict[str, Union[str, int, float]], optional
            search filter criteria as key-value pairs, where *key* is a keyword
            or a tag of the attribute and *value* is the expected value that
            should match

        Returns
        -------
        List[Dict[str, dict]]
            matching instances

        '''
        url = self._get_series_url(study_instance_uid)
        params = self._parse_query_parameters(
            fuzzymatching, limit, offset, fields, **search_filters
        )
        series = self._http_get_application_json(url, **params)
        if series is None:
            return []
        if not(isinstance(series, list)):
            series = [series]
        return series

    def retrieve_series(self, study_instance_uid, series_instance_uid):
        '''Retrieves instances of a given DICOM series.

        Parameters
        ----------
        study_instance_uid: str
            unique study identifier
        series_instance_uid: str
            unique series identifier

        Returns
        -------
        List[pydicom.dataset.Dataset]
            data sets

        '''
        if study_instance_uid is None:
            raise ValueError('Study UID is required for retrieval of series.')
        if series_instance_uid is None:
            raise ValueError('Series UID is required for retrieval of series.')
        url = self._get_series_url(study_instance_uid, series_instance_uid)
        return self._http_get_multipart_application_dicom(url)

    def retrieve_series_metadata(self, study_instance_uid, series_instance_uid):
        '''Retrieves metadata for instances of a given DICOM series.

        Parameters
        ----------
        study_instance_uid: str
            unique study identifier
        series_instance_uid: str
            unique series identifier

        Returns
        -------
        Dict[str, dict]
            metadata in DICOM JSON format

        '''
        if study_instance_uid is None:
            raise ValueError(
                'Study UID is required for retrieval of series metadata.'
            )
        if series_instance_uid is None:
            raise ValueError(
                'Series UID is required for retrieval of series metadata.'
            )
        url = self._get_series_url(study_instance_uid, series_instance_uid)
        url += '/metadata'
        return self._http_get_application_json(url)

    def search_instances(self, study_instance_uid=None,
                         series_instance_uid=None, fuzzymatching=None,
                         limit=None, offset=None, fields=None,
                         search_filters={}):
        '''Searches DICOM instances.

        Parameters
        ----------
        study_instance_uid: str, optional
            unique study identifier
        series_instance_uid: str, optional
            unique series identifier
        fuzzymatching: bool, optional
            whether fuzzy semantic matching should be performed
        limit: int, optional
            maximum number of results that should be returned
        offset: int, optional
            number of results that should be skipped
        fields: Union[list, tuple, set], optional
            names of fields (attributes) that should be included in results
        search_filters: Dict[str, Union[str, int, float]], optional
            search filter criteria as key-value pairs, where *key* is a keyword
            or a tag of the attribute and *value* is the expected value that
            should match

        Returns
        -------
        List[Dict[str, dict]]
            matching instances

        '''
        url = self._get_instances_url(study_instance_uid, series_instance_uid)
        params = self._parse_query_parameters(
            fuzzymatching, limit, offset, fields, **search_filters
        )
        instances = self._http_get_application_json(url, **params)
        if instances is None:
            return []
        if not(isinstance(instances, list)):
            instances = [instances]
        return instances

    def retrieve_instance(self, study_instance_uid, series_instance_uid,
                          sop_instance_uid):
        '''Retrieves an individual DICOM instance.

        Parameters
        ----------
        study_instance_uid: str
            unique study identifier
        series_instance_uid: str
            unique series identifier
        sop_instance_uid: str
            unique instance identifier

        Returns
        -------
        pydicom.dataset.Dataset
            data set

        '''

        if study_instance_uid is None:
            raise ValueError(
                'Study UID is required for retrieval of instance.'
            )
        if series_instance_uid is None:
            raise ValueError(
                'Series UID is required for retrieval of instance.'
            )
        if sop_instance_uid is None:
            raise ValueError(
                'Instance UID is required for retrieval of instance.'
            )
        url = self._get_instances_url(
            study_instance_uid, series_instance_uid, sop_instance_uid
        )
        return self._http_get_multipart_application_dicom(url)[0]

    def store_instances(self, datasets, study_instance_uid=None):
        '''Stores DICOM instances.

        Parameters
        ----------
        datasets: List[pydicom.dataset.Dataset]
            instances that should be stored
        study_instance_uid: str, optional
            unique study identifier

        '''
        url = self._get_studies_url(study_instance_uid)
        self._http_post_multipart_application_dicom(url, datasets)

    def retrieve_instance_metadata(self, study_instance_uid,
                                   series_instance_uid, sop_instance_uid):
        '''Retrieves metadata of an individual DICOM instance.

        Parameters
        ----------
        study_instance_uid: str
            unique study identifier
        series_instance_uid: str
            unique series identifier
        sop_instance_uid: str
            unique instance identifier

        Returns
        -------
        Dict[str, dict]
            metadata in DICOM JSON format

        '''
        if study_instance_uid is None:
            raise ValueError(
                'Study UID is required for retrieval of instance metadata.'
            )
        if series_instance_uid is None:
            raise ValueError(
                'Series UID is required for retrieval of instance metadata.'
            )
        if sop_instance_uid is None:
            raise ValueError(
                'Instance UID is required for retrieval of instance metadata.'
            )
        url = self._get_instances_url(
            study_instance_uid, series_instance_uid, sop_instance_uid
        )
        url += '/metadata'
        return self._http_get_application_json(url)

    def retrieve_instance_frames_rendered(self, study_instance_uid,
                                          series_instance_uid,
                                          sop_instance_uid, frame_numbers,
                                          compression='jpeg'):
        '''Retrieves compressed frame items of pixel data element of an
        individual DICOM instance.

        Parameters
        ----------
        study_instance_uid: str
            unique study identifier
        series_instance_uid: str
            unique series identifier
        sop_instance_uid: str
            unique instance identifier
        frame_numbers: List[int]
            one-based positional indices of the frames within the instance
        compression: str, optional
            name of the image compression format
            (default:``"jpeg"``, options: ``{"jpeg", "png"}``)

        Returns
        -------
        PIL.Image.Image
            image

        '''
        if study_instance_uid is None:
            raise ValueError(
                'Study UID is required for retrieval of instance frames.'
            )
        if series_instance_uid is None:
            raise ValueError(
                'Series UID is required for retrieval of instance frames.'
            )
        if sop_instance_uid is None:
            raise ValueError(
                'Instance UID is required for retrieval of instance frames.'
            )
        if compression not in {'jpeg', 'png'}:
            raise ValueError(
                'Compression format "{}" is not supported.'.format(compression)
            )
        url = self._get_instances_url(
            study_instance_uid, series_instance_uid, sop_instance_uid
        )
        params = {'quality': 100}  # TODO: viewport, window
        frame_list = ','.join([str(n) for n in frame_numbers])
        url += '/frames/{frame_list}/rendered'.format(frame_list=frame_list)
        pixeldata = self._http_get_multipart_image(url, compression, **params)
        return [Image.open(BytesIO(d)) for d in pixeldata]

    def retrieve_instance_frames(self, study_instance_uid, series_instance_uid,
                                 sop_instance_uid, frame_numbers):
        '''Retrieves uncompressed frame items of a pixel data element of an
        individual DICOM image instance.

        Parameters
        ----------
        study_instance_uid: str
            unique study identifier
        series_instance_uid: str
            unique series identifier
        sop_instance_uid: str
            unique instance identifier
        frame_numbers: List[int]
            one-based positional indices of the frames within the instance

        Returns
        -------
        PIL.Image.Image
            image

        '''
        if study_instance_uid is None:
            raise ValueError(
                'Study UID is required for retrieval of instance frames.'
            )
        if series_instance_uid is None:
            raise ValueError(
                'Series UID is required for retrieval of instance frames.'
            )
        if sop_instance_uid is None:
            raise ValueError(
                'Instance UID is required for retrieval of instance frames.'
            )
        # First retrieve metadata to determine dimensions of image
        metadata = self.retrieve_instance_metadata(
            study_instance_uid, series_instance_uid, sop_instance_uid
        )
        dataset = load_json_dataset(metadata)
        mode = _map_color_mode(dataset.PhotometricInterpretation)
        size = (dataset.Columns, dataset.Rows)
        url = self._get_instances_url(
            study_instance_uid, series_instance_uid, sop_instance_uid
        )
        frame_list = ','.join([str(n) for n in frame_numbers])
        url += '/frames/{frame_list}'.format(frame_list=frame_list)
        pixeldata = self._http_get_multipart_application_octet_stream(url)
        return [Image.frombytes(mode, size, d) for d in pixeldata]

    @staticmethod
    def lookup_keyword(tag):
        '''Looks up the keyword of a DICOM attribute.

        Parameters
        ----------
        tag: Union[str, int, Tuple[str], pydicom.tag.Tag]
            attribute tag (e.g. ``"00080018"``)

        Returns
        -------
        str
            attribute keyword (e.g. ``"SOPInstanceUID"``)

        '''
        return pydicom.datadict.keyword_for_tag(tag)

    @staticmethod
    def lookup_tag(keyword):
        '''Looks up the tag of a DICOM attribute.

        Parameters
        ----------
        keyword: str
            attribute keyword (e.g. ``"SOPInstanceUID"``)

        Returns
        -------
        str
            attribute tag as HEX string (e.g. ``"00080018"``)

        '''
        tag = pydicom.datadict.tag_for_keyword(keyword)
        tag = pydicom.tag.Tag(tag)
        return '{0:04x}{1:04x}'.format(tag.group, tag.element)