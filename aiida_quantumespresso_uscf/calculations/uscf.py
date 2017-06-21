# -*- coding: utf-8 -*-

import os
from aiida.common.utils import classproperty
from aiida.common.exceptions import InputValidationError, ValidationError, UniquenessError, NotExistent
from aiida.common.datastructures import CalcInfo, CodeInfo
from aiida.orm.data.remote import RemoteData
from aiida.orm.data.parameter import ParameterData
from aiida.orm.data.array.kpoints import KpointsData
from aiida.orm.calculation.job import JobCalculation
from aiida_quantumespresso.calculations import get_input_data_text, _lowercase_dict, _uppercase_dict
from aiida_quantumespresso.calculations.pw import PwCalculation

class UscfCalculation(JobCalculation):
    """
    Uscf (HUBBARD) calculation for Quantum ESPRESSO
    """
    def _init_internal_params(self):
        super(UscfCalculation, self)._init_internal_params()

        self._PREFIX = 'aiida'
        self._INPUT_FILE_NAME = 'aiida.in'
        self._OUTPUT_FILE_NAME = 'aiida.out'
        self._OUTPUT_CHI_SUFFIX = '.chi.dat'
        self._OUTPUT_HUBBARD_SUFFIX = '.Hubbard_U.dat'

        self._default_parser = 'quantumespresso.uscf'
        self._compulsory_namelists = ['INPUTUSCF']

        # Keywords that cannot be set manually, only by the plugin
        self._blocked_keywords = [
            ('INPUTUSCF', 'iverbosity'),
            ('INPUTUSCF', 'prefix'),
            ('INPUTUSCF', 'outdir'),
            ('INPUTUSCF', 'nq1'),
            ('INPUTUSCF', 'nq2'),
            ('INPUTUSCF', 'nq3'),
            ('INPUTUSCF', 'conv_thr_chi'),
        ]

    @classproperty
    def _OUTPUT_SUBFOLDER(cls):
        return './out/'

    @classproperty
    def _use_methods(cls):
        """
        Additional use_* methods for the Uscf calculation class
        """
        retdict = JobCalculation._use_methods
        retdict.update({
            'settings': {
                'valid_types': ParameterData,
                'additional_parameter': None,
                'linkname': 'settings',
                'docstring': 'Use an additional node for special settings',
            },
            'parameters': {
                'valid_types': ParameterData,
                'additional_parameter': None,
                'linkname': 'parameters',
                'docstring': ('Use a node that specifies the input parameters for the namelists'),
            },
            'parent_folder': {
                'valid_types': RemoteData,
                'additional_parameter': None,
                'linkname': 'parent_calc_folder',
                'docstring': ('Use a remote folder as parent folder (for restarts and similar'),
            },
            'qpoints': {
                'valid_types': KpointsData,
                'additional_parameter': None,
                'linkname': 'qpoints',
                'docstring': ('Specify the Qpoints on which to compute phonons'),
            },
        })
        return retdict

    def _prepare_for_submission(self, tempfolder, inputdict):        
        """
        This method will create the raw input files for the calculation
        
        :param tempfolder: an aiida.common.folders.Folder instance representing
                           a path on disk where input files are to be written to
        :param inputdict: a dictionary with the input nodes
        """
        try:
            code = inputdict.pop(self.get_linkname('code'))
        except KeyError:
            raise InputValidationError("No code specified for this calculation")

        try:
            parameters = inputdict.pop(self.get_linkname('parameters'))
        except KeyError:
            raise InputValidationError("No parameters specified for this calculation")
        if not isinstance(parameters, ParameterData):
            raise InputValidationError("parameters is not of type ParameterData")

        try:
            qpoints = inputdict.pop(self.get_linkname('qpoints'))
        except KeyError:
            raise InputValidationError("No qpoints specified for this calculation")
        if not isinstance(qpoints, KpointsData):
            raise InputValidationError("qpoints is not of type KpointsData")

        try:
            parent_calc_folder = inputdict.pop(self.get_linkname('parent_folder'))
        except KeyError:
            raise InputValidationError("No parent_folder specified for this calculation")
        if not isinstance(parent_calc_folder, RemoteData):
            raise InputValidationError("parent_folder is not of type RemoteData")

        # Settings can be undefined, and defaults to an empty dictionary.
        settings = inputdict.pop(self.get_linkname('settings'), ParameterData(dict={}))
        if not isinstance(settings, ParameterData):
            raise InputValidationError("settings must be of type ParameterData")
        settings_dict = _uppercase_dict(settings.get_dict(), dict_name='settings')

        # Validate the parent_folder input node
        parents = parent_calc_folder.get_inputs(node_type=JobCalculation)
        if len(parents) != 1:
            raise UniquenessError("input 'parent_folder'<{}> has more than one parents"
                .format(parent_calc_folder.pk))
        parent_calc = parents[0]

        if not isinstance(parent_calc, PwCalculation):
            raise ValueError("parent calculation must be a PwCalculation")

        # Parent calculation must be on the same computer
        comp_current = self.get_computer()
        comp_parent = parent_calc.get_computer()
        if not comp_current.uuid == comp_parent.uuid:
            raise InputValidationError("calculation has to be run on the same computer"
                " as that of the parent calculation: {}".format(comp_parent.get_name()))

        # Determine the path of the output folder of the parent calculation
        try:
            default_parent_output_folder = parent_calc._OUTPUT_SUBFOLDER
        except AttributeError:
            try:
                default_parent_output_folder = parent_calc._get_output_folder()
            except AttributeError:
                raise InputValidationError("parent calculation does not have a default output subfolder")

        parent_calc_out_subfolder = settings_dict.pop('PARENT_CALC_OUT_SUBFOLDER', default_parent_output_folder)      

        # Any remaining input nodes are not recognized
        if inputdict:
            raise InputValidationError("the following input nodes are unrecognized: {}".format(inputdict.keys()))

        # Transform first-level keys (i.e. namelist and card names) to uppercase and second-level to lowercase
        input_params = _uppercase_dict(parameters.get_dict(), dict_name='parameters')
        input_params = {k: _lowercase_dict(v, dict_name=k) for k, v in input_params.iteritems()}

        # Check for presence of blocked keywords
        for nl, flag in self._blocked_keywords:
            if nl in input_params:
                if flag in input_params[nl]:
                    raise InputValidationError("explicit definition of the '{}' "
                        "flag in the '{}' namelist or card is not allowed".format(flag, nl))

        if 'INPUTUSCF' not in input_params:
            raise InputValidationError("No namelist INPUTUSCF found in input")

        input_params['INPUTUSCF']['outdir'] = self._OUTPUT_SUBFOLDER
        input_params['INPUTUSCF']['prefix'] = self._PREFIX
        input_params['INPUTUSCF']['iverbosity'] = 2

        # Validate qpoint inputs
        try:
            mesh, offset = qpoints.get_kpoints_mesh()

            if any([i != 0. for i in offset]):
                raise NotImplementedError("support for qpoint meshes with non-zero offsets are not implemented")

            input_params['INPUTUSCF']['nq1'] = mesh[0]
            input_params['INPUTUSCF']['nq2'] = mesh[1]
            input_params['INPUTUSCF']['nq3'] = mesh[2]

        except AttributeError:
            raise NotImplementedError("support for explicit qpoints is not implemented, only uniform meshes")

        # Creating the input file
        input_filename = tempfolder.get_abs_path(self._INPUT_FILE_NAME)
        with open(input_filename, 'w') as handle:
            for namelist_name in self._compulsory_namelists:
                handle.write("&{0}\n".format(namelist_name))
                namelist = input_params.pop(namelist_name, {})
                for k, v in sorted(namelist.iteritems()):
                    handle.write(get_input_data_text(k, v))
                handle.write("/\n")
            
        if input_params:
            raise InputValidationError('the following specified namelists are invalid {}'
                .format(','.join(input_params.keys())))


        # Empty command line by default
        cmdline_params = settings_dict.pop('CMDLINE', [])
        uuid_computer = parent_calc_folder.get_computer().uuid

        codeinfo = CodeInfo()
        codeinfo.cmdline_params = (list(cmdline_params) + ["-in", self._INPUT_FILE_NAME])
        codeinfo.stdout_name = self._OUTPUT_FILE_NAME
        codeinfo.code_uuid = code.uuid

        calcinfo = CalcInfo()
        calcinfo.uuid = self.uuid
        calcinfo.codes_info = [codeinfo]
        calcinfo.retrieve_list = []
        calcinfo.local_copy_list = []
        calcinfo.remote_copy_list = []

        # Retrieve by default the output file
        calcinfo.retrieve_list.append(self._OUTPUT_FILE_NAME)
        calcinfo.retrieve_list.append(self._PREFIX + self._OUTPUT_CHI_SUFFIX)
        calcinfo.retrieve_list.append(self._PREFIX + self._OUTPUT_HUBBARD_SUFFIX)

        # Output folder of parent calculation
        path_src = os.path.join(parent_calc_folder.get_remote_path(), parent_calc_out_subfolder)
        path_dst = self._OUTPUT_SUBFOLDER
        calcinfo.remote_copy_list.append((uuid_computer, path_src, path_dst))

        if settings_dict:
            raise InputValidationError('the following specified keys in the settings node are invalid {}'
                ','.join(settings_dict.keys()))

        return calcinfo
    
    def use_parent_calculation(self, calculation):
        """
        Set the parent calculation from which it will inherit the output subfolder.
        A link will be created from this calculation to the parent RemoteData
        """
        if not isinstance(calculation, PwCalculation):
            raise ValueError("parent calculation must be a PwCalculation")
        
        remotedata = calculation.get_outputs(type=RemoteData)

        if not remotedata:
            raise NotExistent("no output RemoteData found for parent calculation")

        if len(remotedata) != 1:
            raise UniquenessError("more than one output RemoteData found for parent calculation")

        self._set_parent_remotedata(remotedata[0])

    def _set_parent_remotedata(self, remotedata):
        """
        Sets the remote folder pointing to the parent PwCalculation
        """
        if not isinstance(remotedata, RemoteData):
            raise ValueError('remotedata must be of type RemoteData')
        
        # Input RemoteData has to be unique
        input_remote = self.get_inputs(node_type=RemoteData)
        if input_remote:
            raise ValidationError('a RemoteData link was already set for this calculation')

        self.use_parent_folder(remotedata)