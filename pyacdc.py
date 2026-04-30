# pyAC-DC.py
# Programa para a medição de diferença RF-AC em conversores térmicos (TCs)
# O programa aceita TCs com saída em tensão, frequência e resistência.
# modificado em outubro de 2023 para calibrar TVCs Fluke A55 acima de 1 MHz
# usando gerador Keysight 33600A como fonte (AC e RF)
# usando dois DVMs (std e dut)
#-------------------------------------------------------------------------------
# Autor:       Gean Marcos Geronymo
#
# Versão inicial:      10-Jun-2016
# Última modificação:  06-Nov-2023
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Nomenclatura de variáveis:
#
# Adotou-se a convenção de utilizar X para as variáveis referentes ao padrão
# e Y para as variáveis referentes ao objeto.
#
# Por exemplo:
#
# Xac - leitura do padrão (std) quando submetido a Vac
# Xdc - leitura do padrão (std) quando submetido a Vdc
# Yac - leitura do objeto (dut) quando submetido a Vac
# Ydc - leitura do objeto (dut) quando submetido a Vdc
#
# O instrumento que lê a saída do padrão é identificado
# como 'std' e o instrumento que lê a saída do objeto
# como 'dut'.
#
# Comandos da chave
# os comandos sao enviados em formato ASCII puro
# utilizar os comandos
# sw.write_raw(chr(2)) (reset)
# sw.write_raw(chr(4)) (ac)
# sw.write_raw(chr(6)) (dc)
# chr(argumento) converte o valor binario em ascii
#-------------------------------------------------------------------------------
# versão do programa
versao = '0.1';
#-------------------------------------------------------------------------------
# Carregar módulos
import pyvisa as visa
import datetime
import configparser
import time
import numpy
import csv
import argparse
import threading
import sys
import select
import termios
import tty
from flask import Flask, jsonify, request
import requests
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Constantes e variáveis globais
# comandos da chave (em ASCII puro)
reset = chr(2)
ac = chr(4)
dc = chr(6)
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Configurações
#-------------------------------------------------------------------------------
# o arquivo settings.ini reune as configurações que podem ser alteradas
config = configparser.ConfigParser() # iniciar o objeto config
config.read('config.ini') # ler o arquivo de configuracao
wait_time = int(config['Measurement Config']['wait_time']); # tempo de espera
heating_time = int(config['Measurement Config']['aquecimento']); # tempo de aquecimento
rm = visa.ResourceManager('@py')
repeticoes = int(config['Measurement Config']['repeticoes']); # quantidade de repetições
vac_nominal = float(config['Measurement Config']['voltage']); # Tensão nominal RF (>= 1 MHz)
vdc_nominal = float(config['Measurement Config']['voltage']); # Tensão nominal AC (100 kHz)
freq_array = config['Measurement Config']['frequency'].split(',') # Array com as frequências
r_dut = float(config['Measurement Config']['r_dut'])
r_std = float(config['Measurement Config']['r_std'])
delta_max_ppm = float(config['Measurement Config'].get('delta_max_ppm', '150'))
measurement_cycle = config['Measurement Config'].get('measurement_cycle', 'RF-AC-RF-AC-RF').strip().upper()
use_bme280 = config.getboolean('Misc', 'use_bme280', fallback=False)
observacoes = config['Misc'].get('observacoes', '')
std_model = config['Instruments'].get('std', '2182A').strip().upper()
dut_model = config['Instruments'].get('dut', '2182A').strip().upper()
if config.has_section('Sources'):
    source_mode = config['Sources'].get('mode', 'shared').strip().lower()
    ac_source_model = config['Sources'].get('ac_source', '33600A').strip().upper()
    rf_source_model = config['Sources'].get('rf_source', '33600A').strip().upper()
else:
    source_mode = 'shared'
    ac_source_model = '33600A'
    rf_source_model = '33600A'
load = str(int(1 / ( (1/r_dut) + (1/r_std) )))
auth_token = config['Security'].get('token', '').strip() if config.has_section('Security') else ''
tvc_std_model = config['TVC'].get('std_model', '') if config.has_section('TVC') else ''
tvc_std_serial = config['TVC'].get('std_serial', '') if config.has_section('TVC') else ''
tvc_dut_model = config['TVC'].get('dut_model', '') if config.has_section('TVC') else ''
tvc_dut_serial = config['TVC'].get('dut_serial', '') if config.has_section('TVC') else ''

if measurement_cycle == 'RF-AC-RF-AC-RF':
    cycle_sequence = ['RF', 'AC', 'RF', 'AC', 'RF']
elif measurement_cycle == 'AC-RF-AC':
    cycle_sequence = ['AC', 'RF', 'AC']
else:
    raise NameError('Ciclo de medicao invalido. Use RF-AC-RF-AC-RF ou AC-RF-AC em Measurement Config/measurement_cycle.')

rf_indices = [i for i, c in enumerate(cycle_sequence) if c == 'RF']
ac_indices = [i for i, c in enumerate(cycle_sequence) if c == 'AC']
cycle_csv_labels = ['RF' if c == 'RF' else 'AC 100 kHz' for c in cycle_sequence]

if use_bme280:
    import smbus2
    import bme280

console = Console()
ui = None
stop_event = threading.Event()


class MeasurementStopped(Exception):
    pass


def recompute_runtime_values():
    global load, freq_array
    load = str(int(1 / ((1/r_dut) + (1/r_std))))
    freq_array = config['Measurement Config']['frequency'].split(',')


class MeasurementUI:
    def __init__(self, enable_live=True):
        self.enable_live = enable_live
        self.status = "Inicializando..."
        self.wait_message = "-"
        self.current_frequency = "-"
        self.current_vdc = "-"
        self.current_vac = "-"
        self.current_repeat = "-"
        self.total_repeats = repeticoes
        self.n_std = "-"
        self.n_dut = "-"
        self.programmed_frequencies_mhz = []
        self.programmed_vdc = vdc_nominal
        self.programmed_vac = vac_nominal
        self.cycle_rows = []
        self.results_rows = []
        self.summary_rows = []
        self.commands = ["start", "stop", "status", "help", "quit"]
        self.command_input = ""
        self.live = None

    def start(self):
        if self.enable_live:
            self.live = Live(self.render(), refresh_per_second=4, auto_refresh=False, console=console)
            self.live.start()

    def stop(self):
        if self.live is not None:
            self.live.stop()
            self.live = None

    def set_status(self, message):
        self.status = message
        self.refresh()

    def set_wait(self, message):
        self.wait_message = message
        self.refresh()

    def clear_wait(self):
        self.wait_message = "-"
        self.refresh()

    def set_frequency(self, frequency_mhz):
        self.current_frequency = "{:.0f} MHz".format(frequency_mhz)
        self.start_cycle_table()
        self.results_rows = []
        self.refresh()

    def start_cycle_table(self, first_std=None, first_dut=None):
        self.cycle_rows = []
        for label in cycle_csv_labels:
            self.cycle_rows.append({'cycle': label, 'std': None, 'dut': None})
        if first_std is not None and first_dut is not None and self.cycle_rows:
            self.cycle_rows[0]['std'] = first_std
            self.cycle_rows[0]['dut'] = first_dut
        self.refresh()

    def set_program(self, frequencies_mhz, vdc_programmed, vac_programmed):
        self.programmed_frequencies_mhz = frequencies_mhz[:]
        self.programmed_vdc = vdc_programmed
        self.programmed_vac = vac_programmed
        self.refresh()

    def set_setpoints(self, current_vdc, current_vac):
        self.current_vdc = "{:.4f} V".format(current_vdc)
        self.current_vac = "{:.4f} V".format(current_vac)
        self.refresh()

    def set_repetition(self, current_repeat, total_repeats):
        self.current_repeat = str(current_repeat)
        self.total_repeats = total_repeats
        self.refresh()

    def set_n_values(self, n_std, n_dut):
        self.n_std = "{:.3f}".format(n_std)
        self.n_dut = "{:.3f}".format(n_dut)
        self.refresh()

    def set_command_input(self, text):
        self.command_input = text
        self.refresh()

    def add_cycle_reading(self, cycle_index, std_value, dut_value):
        if cycle_index < len(self.cycle_rows):
            self.cycle_rows[cycle_index]['std'] = std_value
            self.cycle_rows[cycle_index]['dut'] = dut_value
        self.refresh()

    def add_result(self, dif_value, delta_value, discarded):
        self.results_rows.append({
            'dif': dif_value,
            'delta': delta_value,
            'discarded': discarded,
        })
        self.refresh()

    def add_frequency_summary(self, frequency_mhz, mean_value, std_value):
        self.summary_rows.append({
            'frequency_mhz': frequency_mhz,
            'mean': mean_value,
            'std': std_value,
        })
        self.refresh()

    def refresh(self):
        if self.live is not None:
            self.live.update(self.render(), refresh=True)

    def render(self):
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=16),
            Layout(name="mid", size=18),
            Layout(name="bottom", size=12)
        )

        layout["mid"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=3)
        )

        status_text = "Frequencia atual: {}\nTensao AC atual: {}\nTensao RF atual: {}\nMensagem do sistema:\n{}".format(
            self.current_frequency,
            self.current_vdc,
            self.current_vac,
            self.status,
        )
        program_table = Table(show_header=True, header_style="bold")
        program_table.add_column("Freq. programadas [MHz]", justify="right")
        if self.programmed_frequencies_mhz:
            current = None
            if self.current_frequency != "-":
                current = float(self.current_frequency.split()[0])
            for f in self.programmed_frequencies_mhz:
                label = "{:.0f}".format(f)
                if current is not None and abs(f - current) < 1e-9:
                    label = "[bold cyan]> {} <[/bold cyan]".format(label)
                program_table.add_row(label)
        else:
            program_table.add_row("-")
        program_table.add_row(" ")
        program_table.add_row("Vac nominal: {:.4f} V".format(self.programmed_vdc))
        program_table.add_row("Vrf nominal: {:.4f} V".format(self.programmed_vac))

        top_layout = Layout()
        top_layout.split_row(Layout(name="status", ratio=2), Layout(name="program", ratio=1), Layout(name="params", ratio=1))
        top_layout["status"].update(Panel(status_text, title="Controle das Medicoes", border_style="cyan"))
        top_layout["program"].update(Panel(program_table, title="Programa da Medicao", border_style="yellow"))

        param_table = Table(show_header=False)
        param_table.add_column("k", style="bold")
        param_table.add_column("v", justify="right")
        param_table.add_row("Espera", "[green]{}[/green]".format(self.wait_message))
        param_table.add_row("Repeticoes", "[green]{}/{}[/green]".format(self.current_repeat, self.total_repeats))
        param_table.add_row("n Padrao", self.n_std)
        param_table.add_row("n Objeto", self.n_dut)
        top_layout["params"].update(Panel(param_table, title="Parametros", border_style="white"))
        layout["top"].update(top_layout)

        cycle_table = Table(show_header=True, header_style="bold")
        cycle_table.add_column("Ciclo", justify="left")
        cycle_table.add_column("STD [mV]", justify="left", style="green")
        cycle_table.add_column("DUT [mV]", justify="left", style="green")
        for row in self.cycle_rows:
            std_value = "-" if row['std'] is None else "{:,.6f}".format(row['std']).replace(',', 'X').replace('.', ',').replace('X', '.')
            dut_value = "-" if row['dut'] is None else "{:,.6f}".format(row['dut']).replace(',', 'X').replace('.', ',').replace('X', '.')
            cycle_table.add_row(
                row['cycle'],
                std_value,
                dut_value,
            )
        layout["left"].update(Panel(cycle_table, title="Leituras Instantaneas", border_style="green"))

        results_table = Table(show_header=True, header_style="bold")
        results_table.add_column("Dif. RF-AC [µV/V]", justify="right")
        results_table.add_column("Delta [µV/V]", justify="right")
        results_table.add_column("Status", justify="center")
        for row in self.results_rows:
            status = "[red]DESCARTADO[/red]" if row['discarded'] else "[green]ACEITO[/green]"
            results_table.add_row(
                "{:,.2f}".format(row['dif']).replace(',', 'X').replace('.', ',').replace('X', '.'),
                "{:,.2f}".format(row['delta']).replace(',', 'X').replace('.', ',').replace('X', '.'),
                status,
            )
        layout["right"].update(Panel(results_table, title="Resultados da Medicao", border_style="magenta"))

        bottom_layout = Layout()
        bottom_layout.split_row(Layout(name="summary", ratio=3), Layout(name="commands", ratio=2))

        summary_table = Table(show_header=True, header_style="bold")
        summary_table.add_column("Frequencia [MHz]", justify="right")
        summary_table.add_column("Media RF-AC [µV/V]", justify="right")
        summary_table.add_column("Desvio padrao [µV/V]", justify="right")
        if self.summary_rows:
            for row in self.summary_rows:
                summary_table.add_row(
                    "{:.0f}".format(row['frequency_mhz']),
                    "{:,.2f}".format(row['mean']).replace(',', 'X').replace('.', ',').replace('X', '.'),
                    "{:,.2f}".format(row['std']).replace(',', 'X').replace('.', ',').replace('X', '.'),
                )
        else:
            summary_table.add_row("-", "-", "-")
        bottom_layout["summary"].update(Panel(summary_table, title="Resumo da Medicao", border_style="blue"))

        cmd_table = Table(show_header=False)
        cmd_table.add_column("c")
        cmd_table.add_row("Comandos: {}".format(", ".join(self.commands)))
        cmd_table.add_row("comando > {}".format(self.command_input))
        bottom_layout["commands"].update(Panel(cmd_table, title="Controle", border_style="white"))

        layout["bottom"].update(bottom_layout)
        return layout

    def to_dict(self):
        return {
            'status': self.status,
            'wait_message': self.wait_message,
            'current_frequency': self.current_frequency,
            'current_vdc': self.current_vdc,
            'current_vac': self.current_vac,
            'current_repeat': self.current_repeat,
            'total_repeats': self.total_repeats,
            'n_std': self.n_std,
            'n_dut': self.n_dut,
            'programmed_frequencies_mhz': self.programmed_frequencies_mhz,
            'programmed_vdc': self.programmed_vdc,
            'programmed_vac': self.programmed_vac,
            'cycle_rows': self.cycle_rows,
            'results_rows': self.results_rows,
            'summary_rows': self.summary_rows,
            'commands': self.commands,
            'command_input': self.command_input,
        }

    def load_dict(self, data):
        self.status = data.get('status', self.status)
        self.wait_message = data.get('wait_message', self.wait_message)
        self.current_frequency = data.get('current_frequency', self.current_frequency)
        self.current_vdc = data.get('current_vdc', self.current_vdc)
        self.current_vac = data.get('current_vac', self.current_vac)
        self.current_repeat = data.get('current_repeat', self.current_repeat)
        self.total_repeats = data.get('total_repeats', self.total_repeats)
        self.n_std = data.get('n_std', self.n_std)
        self.n_dut = data.get('n_dut', self.n_dut)
        self.programmed_frequencies_mhz = data.get('programmed_frequencies_mhz', self.programmed_frequencies_mhz)
        self.programmed_vdc = data.get('programmed_vdc', self.programmed_vdc)
        self.programmed_vac = data.get('programmed_vac', self.programmed_vac)
        self.cycle_rows = data.get('cycle_rows', self.cycle_rows)
        self.results_rows = data.get('results_rows', self.results_rows)
        self.summary_rows = data.get('summary_rows', self.summary_rows)
        self.commands = data.get('commands', self.commands)
        self.command_input = data.get('command_input', self.command_input)
        self.refresh()

#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Definições das funções
#-------------------------------------------------------------------------------
# função espera(segundos)
# aceita como parâmetro o tempo de espera, em segundos
# hack para poder interromper o programa a qualquer momento
# no Windows XP, a função time.sleep não pode ser interrompida por uma
# interrupção de teclado. A função quebra a chamada dessa função em várias
# chamadas de 0,1 segundo.
def espera(segundos):
    remaining = int(segundos)
    while remaining > 0:
        if stop_event.is_set():
            raise MeasurementStopped()
        if ui is not None:
            ui.set_wait("{} s".format(remaining))
        time.sleep(1)
        remaining -= 1
    if ui is not None:
        ui.clear_wait()
    return
#-------------------------------------------------------------------------------
# inicializar bme280
def bme280_init():
    global port;
    port = 1;
    global address;
    address = 0x76;
    global bus;
    bus = smbus2.SMBus(port);
    global calibration_params;
    calibration_params = bme280.load_calibration_params(bus, address)

    return

def bme280_read():
    return bme280.sample(bus, address, calibration_params)

def configure_voltmeter(meter, model):
    if model == '182A':
        meter.write("X")
        meter.write("R0I0B1X")
        meter.write("O1P2X")
        print("Keithley 182A...\n")
    elif model == '2182A':
        meter.write("SENS:CHAN 1")
        meter.write(":SENS:VOLT:CHAN1:RANG:AUTO ON")
        meter.write(":SENS:VOLT:NPLC 18")
        meter.write(":SENS:VOLT:DIG 8")
    print(meter.query("*IDN?"))
    print("OK!\n")

def read_voltmeter(meter, model):
    if model == '182A':
        return meter.query("X")
    if model == '2182A':
        return meter.query(":FETCH?")
    try:
        return meter.query("READ?")
    except Exception:
        return meter.query(":FETCH?")

def set_ac_voltage_and_frequency(voltage, frequency=100000):
    if source_mode == 'shared':
        if ac_source_model == '33600A':
            ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(voltage))
            ac_source.write("SOUR1:FREQ {:.0f}".format(frequency))
        else:
            raise NameError('Modo shared suporta apenas fonte 33600A.')
    else:
        if ac_source_model == '33600A':
            ac_source.write("SOUR1:VOLT {:.3f} VRMS".format(voltage))
            ac_source.write("SOUR1:FREQ {:.0f}".format(frequency))
        elif ac_source_model == '5700A':
            ac_source.write("OUT {:.6f} V, {:.0f} HZ".format(voltage, frequency))
        else:
            raise NameError('Modelo de fonte AC não suportado: {}'.format(ac_source_model))

def set_rf_voltage_and_frequency(voltage, frequency):
    if source_mode == 'shared':
        if rf_source_model == '33600A':
            ac_source.write("SOUR2:VOLT {:.3f} VRMS".format(voltage))
            ac_source.write("SOUR2:FREQ {:.0f}".format(frequency))
        else:
            raise NameError('Modo shared suporta apenas fonte 33600A no canal RF.')
    else:
        if rf_source_model == '33600A':
            rf_source.write("SOUR1:VOLT {:.3f} VRMS".format(voltage))
            rf_source.write("SOUR1:FREQ {:.0f}".format(frequency))
        else:
            raise NameError('Modelo de fonte RF não suportado: {}'.format(rf_source_model))

def sources_output_on():
    if source_mode == 'shared':
        ac_source.write("OUTP1 ON")
        ac_source.write("OUTP2 ON")
    else:
        if ac_source_model == '33600A':
            ac_source.write("OUTP1 ON")
        elif ac_source_model == '5700A':
            ac_source.write("OPER")
        if rf_source_model == '33600A':
            rf_source.write("OUTP1 ON")

def sources_output_off():
    if source_mode == 'shared':
        ac_source.write("OUTP1 OFF")
        ac_source.write("OUTP2 OFF")
    else:
        if ac_source_model == '33600A':
            ac_source.write("OUTP1 OFF")
        elif ac_source_model == '5700A':
            ac_source.write("STBY")
        if rf_source_model == '33600A':
            rf_source.write("OUTP1 OFF")

# função instrument_init()
# inicializa a comunicação com os instrumentos, via GPIB
def instrument_init():
    # variáveis globais
    global ac_source;
    global rf_source;
    global std;
    global dut;
    global sw;

    if source_mode == 'shared':
        source_address = config['GPIB'].get('ac_source', config['GPIB'].get('rf_source', '5'))
        print("Comunicando com fonte AC/RF no endereço "+source_address+"...")
        ac_source = rm.open_resource("GPIB0::"+source_address+"::INSTR")
        rf_source = ac_source
        print(ac_source.query("*IDN?"))
        print("OK!\n")
    elif source_mode == 'separate':
        ac_address = config['GPIB'].get('ac_source', '5')
        rf_address = config['GPIB'].get('rf_source', ac_address)
        print("Comunicando com fonte AC no endereço "+ac_address+"...")
        ac_source = rm.open_resource("GPIB0::"+ac_address+"::INSTR")
        print(ac_source.query("*IDN?"))
        print("OK!\n")

        print("Comunicando com fonte RF no endereço "+rf_address+"...")
        rf_source = rm.open_resource("GPIB0::"+rf_address+"::INSTR")
        print(rf_source.query("*IDN?"))
        print("OK!\n")
    else:
        raise NameError("Valor inválido para Sources/mode (use shared ou separate).")

    print("Comunicando com o medidor do padrão no endereço "+config['GPIB']['std']+"...");
    std = rm.open_resource("GPIB0::"+config['GPIB']['std']+"::INSTR");
    configure_voltmeter(std, std_model)

    print("Comunicando com o medidor do objeto no endereço "+config['GPIB']['dut']+"...");
    dut = rm.open_resource("GPIB0::"+config['GPIB']['dut']+"::INSTR");
    configure_voltmeter(dut, dut_model)
 
    print("Comunicando com a chave no endereço "+config['GPIB']['sw']+"...");
    sw = rm.open_resource("GPIB0::"+config['GPIB']['sw']+"::INSTR");
    sw.write_raw(reset);
    print("OK!\n");

    return
#-------------------------------------------------------------------------------
# comandos Agilent 33600A
# OUTP1:LOAD INF
# SOUR1:FUNC SIN
# SOUR1:VOLT 1.0 VRMS
# SOUR1:FREQ +1.0E+05
# OUTP1 ON

# função meas_init()
# inicializa os instrumentos, coloca as fontes em OPERATE, etc.
def meas_init():
    if source_mode == 'shared':
        ac_source.write("*RST")
        ac_source.write("*CLS")
        ac_source.write("OUTP1:LOAD "+load)
        ac_source.write("OUTP2:LOAD "+load)
        ac_source.write("SOUR1:FUNC SIN")
        ac_source.write("SOUR2:FUNC SIN")
    else:
        if ac_source_model == '33600A':
            ac_source.write("*RST")
            ac_source.write("*CLS")
            ac_source.write("OUTP1:LOAD "+load)
            ac_source.write("SOUR1:FUNC SIN")
        elif ac_source_model == '5700A':
            ac_source.write("*RST")
            ac_source.write("*CLS")

        if rf_source_model == '33600A':
            rf_source.write("*RST")
            rf_source.write("*CLS")
            rf_source.write("OUTP1:LOAD "+load)
            rf_source.write("SOUR1:FUNC SIN")

    set_ac_voltage_and_frequency(vdc_nominal, 100000)
    set_rf_voltage_and_frequency(vac_nominal, 1000000)
    # Entrar em OPERATE
    espera(2); # esperar 2 segundos
    sources_output_on()
    espera(10);
    sw.write_raw(ac);
    espera(10);
    return
#-------------------------------------------------------------------------------
# função ler_std()
# retorna uma leitura single-shot da saída do TC padrão
# não aceita parâmetros de entrada
def ler_std():
    return read_voltmeter(std, std_model)
#-------------------------------------------------------------------------------
# função ler_std()
# retorna uma leitura single-shot da saída do TC objeto
# não aceita parâmetros de entrada
def ler_dut():
    return read_voltmeter(dut, dut_model)
#-------------------------------------------------------------------------------
# aceita como parâmetro o vetor com as leituras do padrão
# escreve na tela a última leitura da saída do TC padrão
def print_std(std_readings):
    return
#-------------------------------------------------------------------------------
# aceita como parâmetro o vetor com as leituras do objeto
# escreve na tela a última leitura da saída do TC objeto
def print_dut(dut_readings):
    return
#-------------------------------------------------------------------------------
# função aquecimento()
# aceita como parâmetro o tempo de aquecimento, em segundos
def aquecimento(tempo):
    # executa o aquecimento, mantendo a tensão nominal aplicada pelo tempo
    # (em segundos) definido na variavel "tempo"
    set_ac_voltage_and_frequency(vdc_nominal, 100000)
    sw.write_raw(dc);
    espera(tempo);
    return
#-------------------------------------------------------------------------------
# função n_measure()
# aceita o número de repetições como parâmetro de entrada
# número de repetições DEVE ser par!
# se não for, será executada uma repetição a mais. p. ex.: 3 -> 4
# executa a medição do coeficiente de linearidade "n" do padrão e do objeto
# o algoritmo consiste em aplicar a tensão nominal, a tensão nominal + 1% e
# a tensão nominal -1%, registrando os respectivos valores de saída de padrão
# e objeto
def n_measure(M):
    # testa se M é par, se não for, soma 1 para se tornar par
    if int(M) % 2 != 0:
        M += 1;
    # define as variáveis que armazenam as leituras do padrão e do objeto
    std_readings = []
    dut_readings = []
    # variavel da constante V0 / (Vi-V0)
    k = []
    # aplica o valor nominal de tensão
    set_rf_voltage_and_frequency(vac_nominal, freq)
    set_ac_voltage_and_frequency(vdc_nominal, 100000)
    
    espera(2); # espera 2 segundos
    sw.write_raw(dc);
    print("Vdc nominal: +{:.3f} V".format(vdc_nominal))
    # aguarda pelo tempo de espera configurado
    espera(wait_time);
    # lê as saídas de padrão e objeto, e armazena na variável std_readings e
    # dut_readings
    std_readings.append(ler_std())
    #espera(1)
    dut_readings.append(ler_dut())
    print_std(std_readings);
    print_dut(dut_readings);

    for i in range(1,M+1):
        # determina se i é par ou ímpar
        # se i é impar, v_i = 1,01*vdc_nominal
        # se i é par, v_i = 0,99*vdc_nominal
        if int(i) % 2 == 0:
            Vi = 0.99*vdc_nominal;
            k.append(-100);
        else:
            Vi = 1.01*vdc_nominal;
            k.append(100);

        sw.write_raw(ac);
        espera(2); # esperar 2 segundos
        set_ac_voltage_and_frequency(Vi, 100000)
        espera(2); # esperar 2 segundos
        sw.write_raw(dc);
        print("Vdc nominal + 1%: +{:.3f} V".format(Vi));
        # aguarda pelo tempo de espera configurado
        espera(wait_time);
        # lê as saídas de padrão e objeto, e armazena na variável std_readings e
        # dut_readings
        std_readings.append(ler_std())
        #espera(1)
        dut_readings.append(ler_dut())
        print_std(std_readings);
        print_dut(dut_readings);

    # cálculo do n
    sw.write_raw(ac); # mantém chave em ac durante cálculo

    X0 = float(std_readings[0].strip())

    Y0 = float(dut_readings[0].strip())
        
    del std_readings[0]
    del dut_readings[0]

    Xi = numpy.array([float(a.strip()) for a in std_readings]);

    Yi = numpy.array([float(a.strip()) for a in dut_readings]);

    nX = (Xi/X0 - 1) * k;
    nY = (Yi/Y0 - 1) * k;

    results = [numpy.mean(nX), numpy.std(nX, ddof=1), numpy.mean(nY), numpy.std(nY, ddof=1)];

    # retorna uma lista com vários arrays
    # o array results contém os resultados (média e desvio padrão de nX e nY)
    return {'results':results, 'Xi':Xi, 'X0':X0, 'Yi':Yi, 'Y0':Y0, 'k':k, 'nX':nX, 'nY':nY}
    
#-------------------------------------------------------------------------------
# função measure(vdc_atual, vac_atual, ciclo_ac)
# Executa os ciclos de medição, na sequência AC, +DC, AC, -DC e AC.
# aceita como parâmetros de entrada:
# vdc_atual - valor atual da tensão DC
# vac_atual - valor atual da tensão AC
# ciclo_ac - valor das leituras do último ciclo AC da medida anterior
# se não for a primeira medição, o primeiro ciclo AC aproveita as leituras do
# último ciclo AC da medição anterior
def measure(vdc_atual,vac_atual,ciclo_ac):
    # inicializa arrays de resultados
    std_readings = []
    dut_readings = []
    # configuração da fonte AC (RF)
    set_rf_voltage_and_frequency(vac_atual, freq)
    # configuração da fonte DC (AC 100 kHz)
    set_ac_voltage_and_frequency(vdc_atual, 100000)
    # Iniciar medição
    espera(2); # esperar 2 segundos

    if ui is not None:
        if ciclo_ac == []:
            ui.start_cycle_table()
        else:
            ui.start_cycle_table(
                float(str(ciclo_ac[0]).strip()) * 1000,
                float(str(ciclo_ac[1]).strip()) * 1000,
            )

    for i, cycle_type in enumerate(cycle_sequence):
        cycle_name = "RF" if cycle_type == 'RF' else "AC 100 kHz"
        if i == 0 and (ciclo_ac != []):
            print("Ciclo {}".format(cycle_name))
            if ui is not None:
                ui.set_status("Executando ciclo: {}".format(cycle_name))
            std_readings.append(ciclo_ac[0])
            dut_readings.append(ciclo_ac[1])
            if ui is not None:
                ui.add_cycle_reading(i, float(str(ciclo_ac[0]).strip()) * 1000, float(str(ciclo_ac[1]).strip()) * 1000)
            print_std(std_readings)
            print_dut(dut_readings)
            continue

        if cycle_type == 'RF':
            sw.write_raw(ac)
            print("Ciclo RF")
        else:
            sw.write_raw(dc)
            print("Ciclo AC 100 kHz")

        if ui is not None:
            ui.set_status("Executando ciclo: {}".format(cycle_name))

        espera(wait_time)
        std_readings.append(ler_std())
        dut_readings.append(ler_dut())
        if ui is not None:
            ui.add_cycle_reading(i, float(std_readings[-1].strip()) * 1000, float(dut_readings[-1].strip()) * 1000)
        print_std(std_readings)
        print_dut(dut_readings)

    # retorna as leituras obtidas para o objeto e para o padrão
    return {'std_readings':std_readings, 'dut_readings':dut_readings}
#-------------------------------------------------------------------------------
# função acdc_calc(readings,N,vdc_atual)
# Calcula a diferença RF-AC a partir dos dados obtidos com a funcao measure()
# aceita como parâmetros de entrada:
# readings - array com as leituras obtidas para o padrão e para o objeto
# N - vetor com os valores calculados de N (padrão e objeto)
# vdc_atual - valor de tensão DC ajustado para o último ciclo.
def acdc_calc(readings,N,vdc_atual):
    # x -> padrao; y -> objeto
    print("Calculando diferença RF-AC...")
    n_X = N[0]; # n do padrão
    n_Y = N[2]; # n do objeto
    # extrai os dados de leituras do padrão
    x = numpy.array([float(a.strip()) for a in readings['std_readings']]);
    # extrai os dados de leitura do objeto
    y = numpy.array([float(a.strip()) for a in readings['dut_readings']])
    # calcula Xac, Xdc, Yac e Ydc a partir das leituras brutas
    Xac = numpy.mean(x[rf_indices]);
    Xdc = numpy.mean(x[ac_indices]);
    Yac = numpy.mean(y[rf_indices]);
    Ydc = numpy.mean(y[ac_indices]);
    # Variáveis auxiliares X e Y
    X = Xac/Xdc - 1;
    Y = Yac/Ydc - 1;
    # diferença RF-AC medida:
    # denominador (1 + Y/n_Y) removido; sugestao H. Laiz durante peer review.
    delta_m = 1e6 * (X/n_X - Y/n_Y);
    # critério para repetir a medição - diferença entre Yac e Ydc    
    Delta = 1e6 * ((Yac - Ydc)/Ydc);
    # ajuste da tensão DC para o próximo ciclo
    adj_dc = vdc_atual * (1 + (Yac - Ydc)/(n_Y * Ydc));
    # timestamp de cada medição
    date = datetime.datetime.now();
    timestamp = datetime.datetime.strftime(date, '%d/%m/%Y %H:%M:%S');
    # retorna lista com os arrays de leitura do padrão, objeto, a diferença ac-dc,
    # Delta=Yac-Ydc, o ajuste DC e o horário
    return {'std_readings':x,'dut_readings':y,'dif':delta_m, 'Delta':Delta, 'adj_dc':adj_dc,'timestamp':timestamp}
#-------------------------------------------------------------------------------
# função equilibrio()
# Calcula a tensão de equilíbrio AC no início da sequência de medições
# A função não aceita parâmetros de entrada
def equilibrio():
    dut_readings = []
    set_rf_voltage_and_frequency(vac_nominal, freq)
    set_ac_voltage_and_frequency(vdc_nominal, 100000)
    espera(5) # aguarda 5 segundos antes de iniciar equilibrio
        
    # Aplica o valor nominal
    sw.write_raw(dc);
    print("Vac nominal: +{:.3f} V".format(vdc_nominal))
    espera(wait_time/2);
    set_rf_voltage_and_frequency(0.999*vac_nominal, freq)
    espera(wait_time/2);
    dut_readings.append(ler_dut())
    print_dut(dut_readings);
    # Aplica Vac - 0.1%
    print("Vrf nominal - 0.1%: +{:.3f} V".format(0.999*vac_nominal))
    sw.write_raw(ac);
    espera(wait_time)
    dut_readings.append(ler_dut())
    print_dut(dut_readings);
    sw.write_raw(dc);
    espera(2);
    set_rf_voltage_and_frequency(1.001*vac_nominal, freq)
    espera(2);
    # Aplica Vac + 0.1%
    print("Vrf nominal + 0.1%: +{:.3f} V".format(1.001*vac_nominal))
    sw.write_raw(ac);
    espera(wait_time)
    dut_readings.append(ler_dut())
    print_dut(dut_readings);
    sw.write_raw(dc);
    # cálculo do equilíbrio
    yp = [0.999*vac_nominal, 1.001*vac_nominal]
    
    xp = [float(dut_readings[1].strip()), float(dut_readings[2].strip())]
    xi = float(dut_readings[0].strip())
    # calcula o valor de equilíbrio através de interpolação linear    
    new_ac = numpy.interp(xi,xp,yp);
    # retorna o novo valor de AC
    return new_ac
#-------------------------------------------------------------------------------
# função stop_instruments()
# função chamada para interromper a medição
# não aceita parâmetros de entrada
# coloca as fontes em stand-by
def stop_instruments():
    sw.write_raw(reset);
    espera(1)
    sources_output_off()
    return
#-------------------------------------------------------------------------------
# função criar_registro()
# Cria um novo registro de medição
# Não aceita parâmetros de entrada
def criar_registro():
    date = datetime.datetime.now();
    timestamp_file = datetime.datetime.strftime(date, '%d-%m-%Y_%Hh%Mm');
    timestamp_registro = datetime.datetime.strftime(date, '%d/%m/%Y %H:%M:%S');
    # o nome do registro é criado de forma automática, a partir da data e hora atuais
    registro_filename = "leituras/registro_"+timestamp_file+".csv"
    with open(registro_filename,"w") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        registro.writerow(['pyAC-DC '+versao]);
        registro.writerow(['Registro de Medições']);
        registro.writerow([' ']);
        registro.writerow(['Início da medição',timestamp_registro]);
        registro.writerow(['Tempo de aquecimento [s]',config['Measurement Config']['aquecimento']]);
        registro.writerow(['Tempo de estabilização [s]',config['Measurement Config']['wait_time']]);
        registro.writerow(['Repetições',config['Measurement Config']['repeticoes']]);
        registro.writerow(['Observações',observacoes]);
        registro.writerow(['TVC STD modelo',tvc_std_model]);
        registro.writerow(['TVC STD serie',tvc_std_serial]);
        registro.writerow(['TVC DUT modelo',tvc_dut_model]);
        registro.writerow(['TVC DUT serie',tvc_dut_serial]);
        registro.writerow([' ']);
        registro.writerow([' ']);

    csvfile.close();
    return registro_filename
#-------------------------------------------------------------------------------
# função registro_frequencia(egistro_filename,frequencia,n_value,vac_equilibrio)
# Inicia uma nova frequência no registro de medição
# Aceita os parâmetros
# registro_filename - o nome do registro criado com a função criar_registro()
# frequencia - o valor da frequência que está sendo medida no momento;
# n_value - os valores obtidos de n para padrão e objeto
# vac_equilibrio - a tensão AC de equilíbrio calculada com a funcao equilibrio()
# n_array:
# {'results':results, 'Xi':Xi, 'X0':X0, 'Yi':Yi, 'Y0':Y0, 'k':k, 'nX':nX, 'nY':nY}
def registro_frequencia(registro_filename,frequencia,n_array,vac_equilibrio):
    with open(registro_filename,"a") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        registro.writerow(['Tensão [V]',config['Measurement Config']['voltage'].replace('.',',')]);
        registro.writerow(['Frequência [MHz]',frequencia.replace('.',',')]);
        registro.writerow([' ']); # pular linha
        registro.writerow(['X0',str(n_array['X0']).replace('.',',')]); # valor de X0
        registro.writerow(['Xi'] + [str(i).replace('.',',') for i in n_array['Xi']]); # valores de Xi
        registro.writerow(['k'] + [str(i).replace('.',',') for i in n_array['k']]); # valores de k
        registro.writerow(['nX'] + [str(i).replace('.',',') for i in n_array['nX']]); # valores de nX
        registro.writerow(['nX (média)',str(n_array['results'][0]).replace('.',',')]); # Valor médio de nX
        registro.writerow(['nX (desvio padrão)',str(n_array['results'][1]).replace('.',',')]); # desvio padrão de nX
        registro.writerow([' ']); # pular linha
        registro.writerow(['Y0',str(n_array['Y0']).replace('.',',')]); # valor de X0
        registro.writerow(['Yi'] + [str(i).replace('.',',') for i in n_array['Yi']]); # valores de Yi
        registro.writerow(['k'] + [str(i).replace('.',',') for i in n_array['k']]); # valores de k
        registro.writerow(['nY'] + [str(i).replace('.',',') for i in n_array['nY']]); # valores de nY
        registro.writerow(['nY (média)',str(n_array['results'][2]).replace('.',',')]); # valor médio de nY
        registro.writerow(['nY (desvio padrão)',str(n_array['results'][3]).replace('.',',')]); # desvio padrão de nY
        registro.writerow([' ']); # pular linha
        registro.writerow(['Vac equilíbrio [V]',str(vac_equilibrio).replace('.',',')]); # Vac calculado para o equilíbrio
        registro.writerow([' ']); # pular linha
        # cabeçalho da tabela de medicao
        header = ['Data / hora']
        for label in cycle_csv_labels:
            header.extend([label+' (STD)', label+' (DUT)'])
        header.extend(['Diferença', 'Delta', 'Tensão DC Aplicada'])
        if use_bme280:
            header.extend(['Temperatura [ºC]', 'Umidade Relativa [% u.r.]', 'Pressão Atmosférica [hPa]'])
        registro.writerow(header)

    csvfile.close();
    return
#-------------------------------------------------------------------------------
# função registro_linha(registro_filename,results,vdc_atual)
# salva uma nova linha (medição individual) no registro de medição
# parâmetros:
# registro_filename - o nome do registro criado com a função criar_registro()
# results - array com os resultados
# vdc_atual - tensão DC calculada para a medição atual
#def registro_linha(registro_filename,results,vdc_atual,ca_data):
def registro_linha(registro_filename,results,vdc_atual,ca_data=None):

    # results -> results['std_readings'], results['dut_readings'], results['dif'], results['Delta'], results['adj_dc'] e results['timestamp']
    with open(registro_filename,"a") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        row = [results['timestamp']]
        for std_value, dut_value in zip(results['std_readings'], results['dut_readings']):
            row.extend([str(std_value).replace('.',','), str(dut_value).replace('.',',')])
        row.extend([str(results['dif']).replace('.',','),str(results['Delta']).replace('.',','),str(vdc_atual).replace('.',',')])
        if use_bme280 and ca_data is not None:
            row.extend([str(ca_data.temperature).replace('.',','),str(ca_data.humidity).replace('.',','),str(ca_data.pressure).replace('.',',')])
        registro.writerow(row)


    csvfile.close();
    return
#-------------------------------------------------------------------------------
# função registro_media(registro_filename,diferenca):
# finaliza o registro de medição para cada frequência, escrevendo a média
# e desvio padrão obtidos.
# Aceita os parâmetros:
# registro_filename - o nome do registro criado com a função criar_registro()
# diferenca - array com a média e o desvio padrão calculados
def registro_media(registro_filename,diferenca):
    with open(registro_filename,"a") as csvfile:
        registro = csv.writer(csvfile, delimiter=';',lineterminator='\n')
        registro.writerow([' ']);
        registro.writerow(['Média',str(numpy.mean(diferenca)).replace('.',',')]);
        registro.writerow(['Desvio-padrão',str(numpy.std(diferenca, ddof=1)).replace('.',',')]);
        registro.writerow([' ']);
        registro.writerow([' ']);
    csvfile.close();
    return
#-------------------------------------------------------------------------------

#-------------------------------------------------------------------------------
# Programa principal
#-------------------------------------------------------------------------------
def run_measurement_loop(enable_live=True):
    global ui
    stop_event.clear()
    try:
        global freq
        ui = MeasurementUI(enable_live=enable_live)
        ui.start()
        ui.set_status("Inicializando sistema")
        ui.set_program([float(v.strip()) for v in freq_array], vdc_nominal, vac_nominal)
        ui.set_repetition(0, repeticoes)
        if use_bme280:
            ui.set_status("Inicializando BME280 (condições ambientais)")
            bme280_init()
        ui.set_status("Inicializando instrumentos")
        instrument_init()
        ui.set_status("Colocando fontes em OPERATE")
        meas_init()
        ui.set_status("Criando arquivo de registro")
        filename = criar_registro()
        ui.set_status("Arquivo {} criado".format(filename))
        ui.set_status("Aquecimento")
        aquecimento(heating_time)
        for value in freq_array:
            if stop_event.is_set():
                raise MeasurementStopped()
            freq = float(value) * 1000000
            ui.set_frequency(freq/1e6)
            ui.set_status("Iniciando medicao em {:5.0f} MHz".format(freq/1e6))
            ui.set_status("Medindo N")
            n_array = n_measure(4)
            n_value = n_array['results']
            ui.set_n_values(n_value[0], n_value[2])
            ui.set_status("Calculando equilibrio RF")
            vac_atual = equilibrio()
            ui.set_status("Vrf aplicado: {:5.3f} V".format(vac_atual))
            ui.set_setpoints(vdc_nominal, vac_atual)
            registro_frequencia(filename,value,n_array,vac_atual)
            first_measure = True
            reuse_last_cycle = True
            if vac_atual > 1.1*vac_nominal:
                raise NameError('Tensão AC ajustada perigosamente alta!')
            ui.set_status("Iniciando repeticoes da medicao")
            diff_acdc = []
            vdc_atual = vdc_nominal
            i = 0
            while (i < repeticoes):
                if stop_event.is_set():
                    raise MeasurementStopped()
                ui.set_status("Repeticao {}/{} | Vac {:5.3f} V".format(i+1, repeticoes, vdc_atual))
                ui.set_setpoints(vdc_atual, vac_atual)
                ui.set_repetition(i+1, repeticoes)
                if first_measure:
                    ciclo_ac = []
                    first_measure = False
                else:
                    ciclo_ac = [readings['std_readings'][-1], readings['dut_readings'][-1]] if reuse_last_cycle else []
                readings = measure(vdc_atual,vac_atual,ciclo_ac)
                results = acdc_calc(readings,n_value,vdc_atual)
                ca_data = bme280_read() if use_bme280 else None
                if abs(results['Delta']) > delta_max_ppm:
                    ui.add_result(results['dif'], results['Delta'], True)
                    ui.set_status("Ponto descartado: Delta {:.2f} µV/V > {:.1f} µV/V".format(results['Delta'], delta_max_ppm))
                    reuse_last_cycle = (measurement_cycle != 'AC-RF-AC')
                    if measurement_cycle == 'AC-RF-AC':
                        vdc_atual = results['adj_dc']
                else:
                    ui.add_result(results['dif'], results['Delta'], False)
                    diff_acdc.append(results['dif'])
                    registro_linha(filename,results,vdc_atual,ca_data)
                    reuse_last_cycle = True
                    if measurement_cycle != 'AC-RF-AC':
                        vdc_atual = results['adj_dc']
                    i += 1
                if vdc_atual > 1.1*vdc_nominal:
                    raise NameError('Tensão AC ajustada perigosamente alta!')
            freq_mean = numpy.mean(diff_acdc)
            freq_std = numpy.std(diff_acdc, ddof=1)
            ui.add_frequency_summary(freq/1e6, freq_mean, freq_std)
            ui.set_status("Medição concluída | Média {:.2f} µV/V | DP {:.2f} µV/V".format(freq_mean, freq_std))
            registro_media(filename,diff_acdc)
        stop_instruments()
        ui.set_status("Concluído")
        return True
    except MeasurementStopped:
        try:
            stop_instruments()
        except Exception:
            pass
        if ui is not None:
            ui.set_status("Medição interrompida por comando stop")
        return False
    except Exception:
        try:
            stop_instruments()
        except Exception:
            pass
        import traceback
        traceback.print_exc()
        if ui is not None:
            ui.set_status("Erro durante a medição")
        return False
    finally:
        if enable_live and ui is not None:
            ui.stop()


measurement_thread = None
measurement_lock = threading.Lock()


def is_measurement_running():
    return measurement_thread is not None and measurement_thread.is_alive()


def apply_runtime_config(payload):
    global wait_time, heating_time, repeticoes, vac_nominal, vdc_nominal
    global r_dut, r_std, delta_max_ppm, measurement_cycle
    global std_model, dut_model, source_mode, ac_source_model, rf_source_model
    global use_bme280, observacoes, tvc_std_model, tvc_std_serial, tvc_dut_model, tvc_dut_serial
    for section in ('Sources', 'GPIB', 'TVC', 'Misc', 'Instruments', 'Measurement Config'):
        if not config.has_section(section):
            config.add_section(section)
    if 'std_model' in payload:
        std_model = str(payload['std_model']).strip().upper()
        config['Instruments']['std'] = std_model
    if 'dut_model' in payload:
        dut_model = str(payload['dut_model']).strip().upper()
        config['Instruments']['dut'] = dut_model
    if 'gpib_std' in payload:
        config['GPIB']['std'] = str(payload['gpib_std']).strip()
    if 'gpib_dut' in payload:
        config['GPIB']['dut'] = str(payload['gpib_dut']).strip()
    if 'source_mode' in payload:
        source_mode = str(payload['source_mode']).strip().lower()
        config['Sources']['mode'] = source_mode
    if 'ac_source_model' in payload:
        ac_source_model = str(payload['ac_source_model']).strip().upper()
        config['Sources']['ac_source'] = ac_source_model
    if 'rf_source_model' in payload:
        rf_source_model = str(payload['rf_source_model']).strip().upper()
        config['Sources']['rf_source'] = rf_source_model
    if 'gpib_ac_source' in payload:
        config['GPIB']['ac_source'] = str(payload['gpib_ac_source']).strip()
    if 'gpib_rf_source' in payload:
        config['GPIB']['rf_source'] = str(payload['gpib_rf_source']).strip()
    if 'voltage' in payload:
        vac_nominal = float(payload['voltage'])
        vdc_nominal = float(payload['voltage'])
        config['Measurement Config']['voltage'] = str(payload['voltage'])
    if 'frequency' in payload:
        config['Measurement Config']['frequency'] = str(payload['frequency'])
    if 'r_dut' in payload:
        r_dut = float(payload['r_dut'])
        config['Measurement Config']['r_dut'] = str(payload['r_dut'])
    if 'r_std' in payload:
        r_std = float(payload['r_std'])
        config['Measurement Config']['r_std'] = str(payload['r_std'])
    if 'wait_time' in payload:
        wait_time = int(payload['wait_time'])
        config['Measurement Config']['wait_time'] = str(payload['wait_time'])
    if 'aquecimento' in payload:
        heating_time = int(payload['aquecimento'])
        config['Measurement Config']['aquecimento'] = str(payload['aquecimento'])
    if 'repeticoes' in payload:
        repeticoes = int(payload['repeticoes'])
        config['Measurement Config']['repeticoes'] = str(payload['repeticoes'])
    if 'delta_max_ppm' in payload:
        delta_max_ppm = float(payload['delta_max_ppm'])
        config['Measurement Config']['delta_max_ppm'] = str(payload['delta_max_ppm'])
    if 'measurement_cycle' in payload:
        measurement_cycle = str(payload['measurement_cycle']).strip().upper()
        config['Measurement Config']['measurement_cycle'] = measurement_cycle
    if 'observacoes' in payload:
        observacoes = str(payload['observacoes'])
        config['Misc']['observacoes'] = observacoes
    if 'use_bme280' in payload:
        use_bme280 = str(payload['use_bme280']).strip().lower() in ('1', 'true', 'yes', 'on')
        config['Misc']['use_bme280'] = 'true' if use_bme280 else 'false'
    if 'tvc_std_model' in payload:
        tvc_std_model = str(payload['tvc_std_model']).strip()
        config['TVC']['std_model'] = tvc_std_model
    if 'tvc_std_serial' in payload:
        tvc_std_serial = str(payload['tvc_std_serial']).strip()
        config['TVC']['std_serial'] = tvc_std_serial
    if 'tvc_dut_model' in payload:
        tvc_dut_model = str(payload['tvc_dut_model']).strip()
        config['TVC']['dut_model'] = tvc_dut_model
    if 'tvc_dut_serial' in payload:
        tvc_dut_serial = str(payload['tvc_dut_serial']).strip()
        config['TVC']['dut_serial'] = tvc_dut_serial

    recompute_runtime_values()
    if measurement_cycle == 'RF-AC-RF-AC-RF':
        new_cycle = ['RF', 'AC', 'RF', 'AC', 'RF']
    elif measurement_cycle == 'AC-RF-AC':
        new_cycle = ['AC', 'RF', 'AC']
    else:
        raise NameError('measurement_cycle invalido')

    global cycle_sequence, rf_indices, ac_indices, cycle_csv_labels
    cycle_sequence = new_cycle
    rf_indices = [i for i, c in enumerate(cycle_sequence) if c == 'RF']
    ac_indices = [i for i, c in enumerate(cycle_sequence) if c == 'AC']
    cycle_csv_labels = ['RF' if c == 'RF' else 'AC 100 kHz' for c in cycle_sequence]


def create_backend_app():
    app = Flask(__name__)

    def check_auth():
        if auth_token == '':
            return True
        token = request.headers.get('X-Auth-Token', '')
        return token == auth_token

    @app.get('/')
    def root_endpoint():
        return 'Backend de medição ativo. Use /status, /start, /stop ou rode --mode web em outro computador.'

    @app.get('/status')
    def status_endpoint():
        if not check_auth():
            return jsonify({'ok': False, 'message': 'Nao autorizado'}), 401
        running = is_measurement_running()
        data = ui.to_dict() if ui is not None else {}
        data['running'] = running
        return jsonify(data)

    @app.get('/commands')
    def commands_endpoint():
        if not check_auth():
            return jsonify({'ok': False, 'message': 'Nao autorizado'}), 401
        return jsonify({'commands': ['start', 'stop', 'status', 'help', 'quit']})

    @app.post('/start')
    def start_endpoint():
        if not check_auth():
            return jsonify({'ok': False, 'message': 'Nao autorizado'}), 401
        global measurement_thread
        with measurement_lock:
            if is_measurement_running():
                return jsonify({'ok': False, 'message': 'Medição já em execução'}), 409
            stop_event.clear()
            measurement_thread = threading.Thread(target=run_measurement_loop, kwargs={'enable_live': False}, daemon=True)
            measurement_thread.start()
        return jsonify({'ok': True, 'message': 'Medição iniciada'})

    @app.post('/stop')
    def stop_endpoint():
        if not check_auth():
            return jsonify({'ok': False, 'message': 'Nao autorizado'}), 401
        stop_event.set()
        return jsonify({'ok': True, 'message': 'Comando stop enviado'})

    @app.get('/config')
    def config_get_endpoint():
        if not check_auth():
            return jsonify({'ok': False, 'message': 'Nao autorizado'}), 401
        return jsonify({
            'std_model': std_model,
            'dut_model': dut_model,
            'gpib_std': config['GPIB'].get('std', ''),
            'gpib_dut': config['GPIB'].get('dut', ''),
            'source_mode': source_mode,
            'ac_source_model': ac_source_model,
            'rf_source_model': rf_source_model,
            'gpib_ac_source': config['GPIB'].get('ac_source', ''),
            'gpib_rf_source': config['GPIB'].get('rf_source', ''),
            'voltage': vac_nominal,
            'frequency': config['Measurement Config']['frequency'],
            'r_dut': r_dut,
            'r_std': r_std,
            'wait_time': wait_time,
            'aquecimento': heating_time,
            'repeticoes': repeticoes,
            'delta_max_ppm': delta_max_ppm,
            'measurement_cycle': measurement_cycle,
            'observacoes': observacoes,
            'use_bme280': use_bme280,
            'tvc_std_model': tvc_std_model,
            'tvc_std_serial': tvc_std_serial,
            'tvc_dut_model': tvc_dut_model,
            'tvc_dut_serial': tvc_dut_serial,
        })

    @app.post('/config')
    def config_post_endpoint():
        if not check_auth():
            return jsonify({'ok': False, 'message': 'Nao autorizado'}), 401
        if is_measurement_running():
            return jsonify({'ok': False, 'message': 'Nao e possivel editar configuracao com medicao em execucao'}), 409
        payload = request.get_json(silent=True) or {}
        try:
            apply_runtime_config(payload)
            if ui is not None:
                ui.set_program([float(v.strip()) for v in freq_array], vdc_nominal, vac_nominal)
                ui.set_repetition(0, repeticoes)
            return jsonify({'ok': True, 'message': 'Configuracao atualizada'})
        except Exception as exc:
            return jsonify({'ok': False, 'message': str(exc)}), 400

    return app


def create_web_client_app(server_url, token=''):
    app = Flask(__name__)

    def auth_headers():
        return {'X-Auth-Token': token} if token else {}

    @app.get('/')
    def webui_endpoint():
        return """<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>pyACDC RF - Web Client</title>
<style>
:root{--bg:#0f1318;--panel:#171d24;--line:#2a3441;--text:#e7edf5;--muted:#93a1b2;--ok:#3ecf8e;--bad:#ff6b6b;--acc:#59b0ff}
body{margin:0;font-family:"DejaVu Sans Mono","Consolas",monospace;background:linear-gradient(135deg,#0d1117,#121a23);color:var(--text)}
.wrap{padding:14px;display:grid;gap:12px;grid-template-columns:2fr 1fr 1fr}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px}
h3{margin:0 0 8px 0;font-size:15px;color:#cfe7ff}.row{margin:4px 0;color:var(--muted)}.row b{color:var(--text)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{border-bottom:1px solid #24303d;padding:6px;text-align:right}th:first-child,td:first-child{text-align:left}
.grid2,.grid1{display:grid;gap:12px;grid-column:1/span 3}.grid2{grid-template-columns:1fr 1.4fr}.grid1{grid-template-columns:1fr}
.ok{color:var(--ok);font-weight:bold}.bad{color:var(--bad);font-weight:bold}.hl{color:var(--acc);font-weight:bold}
.cmd{display:flex;gap:8px}input,button,textarea,select{background:#0f151d;color:var(--text);border:1px solid #304055;border-radius:8px;padding:8px 10px}
button{cursor:pointer}.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.foot{color:var(--muted);font-size:12px;margin-top:6px}
#edit_section{display:none}.compact .hide-compact{display:none!important}
@media (max-width:980px){.wrap{grid-template-columns:1fr;padding:10px}.grid2,.grid1{grid-column:1/span 1;grid-template-columns:1fr}.card{padding:8px}table{display:block;overflow:auto;white-space:nowrap;font-size:12px}.cmd{flex-direction:column}input,textarea,select{width:100%;box-sizing:border-box}}
</style></head><body>
<div id='root' class='wrap'>
<div class='card'><h3>Controle das Medicoes</h3><div class='row'>Frequencia atual: <b id='freq'>-</b></div><div class='row'>Tensao AC atual: <b id='vdc'>-</b></div><div class='row'>Tensao RF atual: <b id='vac'>-</b></div><div class='row'>Espera: <b id='wait'>-</b></div><div class='row'>Repeticao: <b id='repetition'>-</b></div><div class='row'>Mensagem: <b id='status'>-</b></div><div class='foot'>Estado: <span id='running'>-</span></div></div>
<div class='card'><h3>Programa da Medicao</h3><div id='freq_list'></div><div class='row'>Vdc nominal: <b id='pvdc'>-</b></div><div class='row'>Vac nominal: <b id='pvac'>-</b></div><div class='row'>n STD: <b id='nstd'>-</b></div><div class='row'>n DUT: <b id='ndut'>-</b></div></div>
<div class='card'><h3>Controle</h3><div class='cmd'><input id='cmd' placeholder='comando > start|stop|status|help|quit' style='flex:1'><button onclick='sendCmd()'>Enviar</button></div><div class='btns'><button onclick="quick('start')">start</button><button onclick="quick('stop')">stop</button><button onclick="quick('status')">status</button><button onclick="quick('help')">help</button><button onclick='toggleCompact()'>modo compacto</button><button onclick='toggleEdit()'>editar configuracoes</button></div><div class='foot' id='help'>Comandos: start, stop, status, help, quit</div></div>
<div class='grid2'><div class='card'><h3>Leituras Instantaneas</h3><table><thead><tr><th>Ciclo</th><th>STD [mV]</th><th>DUT [mV]</th></tr></thead><tbody id='cycles'></tbody></table></div><div class='card'><h3>Resultados da Medicao</h3><table><thead><tr><th>Dif. RF-AC [µV/V]</th><th>Delta [µV/V]</th><th>Status</th></tr></thead><tbody id='results'></tbody></table></div></div>
<div class='grid1 hide-compact'><div class='card'><h3>Tendencia RF-AC</h3><canvas id='trend' style='width:100%;height:180px'></canvas></div></div>
<div class='grid1 hide-compact'><div class='card'><h3>Resumo da Medicao</h3><table><thead><tr><th>Frequencia [MHz]</th><th>Media RF-AC [µV/V]</th><th>Desvio padrao [µV/V]</th></tr></thead><tbody id='summary'></tbody></table></div></div>
<div class='grid1' id='edit_section'><div class='card'><h3>Configuracao dos Instrumentos</h3>
<div class='row'>STD modelo <input id='cfg_std' size='8'> GPIB STD <input id='cfg_gpib_std' size='5'> DUT modelo <input id='cfg_dut' size='8'> GPIB DUT <input id='cfg_gpib_dut' size='5'></div>
<div class='row'>Modo fontes <select id='cfg_source_mode'><option value='shared'>shared</option><option value='separate'>separate</option></select> Fonte AC modelo <input id='cfg_ac_model' size='8'> GPIB AC <input id='cfg_gpib_ac' size='5'></div>
<div class='row'>Fonte RF modelo <input id='cfg_rf_model' size='8'> GPIB RF <input id='cfg_gpib_rf' size='5'></div>
<h3 style='margin-top:12px'>Programa de Medicao</h3>
<div class='row'>Tensao[V] <input id='cfg_voltage' size='8'> Frequencias[MHz] <input id='cfg_freq' size='40'></div>
<div class='row'>r_dut <input id='cfg_rdut' size='8'> r_std <input id='cfg_rstd' size='8'> repeticoes <input id='cfg_rep' size='5'> wait[s] <input id='cfg_wait' size='5'> aquecimento[s] <input id='cfg_heat' size='5'></div>
<div class='row'>delta [µV/V] <input id='cfg_delta' size='7'> ciclo <input id='cfg_cycle' size='20'></div>
<div class='row'>observacoes <input id='cfg_obs' size='48'> use_bme280 <select id='cfg_bme'><option value='false'>false</option><option value='true'>true</option></select></div>
<div class='row'>TVC STD modelo <input id='cfg_tvc_std_model' size='12'> serie <input id='cfg_tvc_std_serial' size='12'> TVC DUT modelo <input id='cfg_tvc_dut_model' size='12'> serie <input id='cfg_tvc_dut_serial' size='12'></div>
<div class='btns'><button onclick='loadConfig()'>Carregar</button><button onclick='saveConfig()'>Salvar</button></div>
</div></div>
</div>
<script>
function fmt(v){return(v===null||v===undefined)?'-':String(v)}
function row(tds){return '<tr>'+tds.map(x=>'<td>'+x+'</td>').join('')+'</tr>'}
function toggleEdit(){const s=document.getElementById('edit_section');s.style.display=(s.style.display==='none'||s.style.display==='')?'grid':'none'}
function toggleCompact(){document.getElementById('root').classList.toggle('compact')}
function fitCanvas(){const c=document.getElementById('trend');const r=c.parentElement;const w=r.clientWidth-20||400;c.style.width=w+'px';c.width=w*2;c.style.height='180px';c.height=360;}function drawTrend(rows,totalReps){const c=document.getElementById('trend'),x=c.getContext('2d');fitCanvas();const w=c.width,h=c.height;const w=c.width,h=c.height;const m={l:50,r:12,t:12,b:30};const pw=w-m.l-m.r,ph=h-m.t-m.b;x.clearRect(0,0,w,h);x.fillStyle='#e7edf5';x.font='12px DejaVu Sans Mono';x.strokeStyle='#2a3441';x.strokeRect(m.l,m.t,pw,ph);const vals=rows.map(r=>Number(r.dif)).filter(v=>Number.isFinite(v));let ymin=-1,ymax=1;if(vals.length){ymin=Math.min(...vals);ymax=Math.max(...vals);if(ymin===ymax){ymin-=1;ymax+=1;}const pad=(ymax-ymin)*0.1;ymin-=pad;ymax+=pad;}const reps=(Number.isFinite(totalReps)&&totalReps>0)?Math.floor(totalReps):Math.max(1,rows.length);const yToPix=v=>m.t+(ymax-v)*(ph/(ymax-ymin));const xToPix=i=>m.l+((i-1)/(Math.max(1,reps-1)))*pw;for(let k=0;k<=4;k++){const v=ymin+(k/4)*(ymax-ymin);const yy=yToPix(v);x.strokeStyle='#223040';x.beginPath();x.moveTo(m.l,yy);x.lineTo(m.l+pw,yy);x.stroke();x.fillStyle='#93a1b2';x.fillText(v.toFixed(2),4,yy+4);}x.fillStyle='#93a1b2';x.fillText('Dif. RF-AC [µV/V]',6,10);x.fillText('Repeticao',m.l+pw/2-30,h-6);x.strokeStyle='#304055';x.beginPath();x.moveTo(m.l,m.t+ph);x.lineTo(m.l+pw,m.t+ph);x.stroke();const xticks=Math.min(reps,10);for(let t=1;t<=xticks;t++){const idx=Math.round(1+(t-1)*(reps-1)/Math.max(1,xticks-1));const xx=xToPix(idx);x.strokeStyle='#304055';x.beginPath();x.moveTo(xx,m.t+ph);x.lineTo(xx,m.t+ph+4);x.stroke();x.fillStyle='#93a1b2';x.fillText(String(idx),xx-6,m.t+ph+16);}rows.forEach((r,ix)=>{const v=Number(r.dif);if(!Number.isFinite(v))return;const xx=xToPix(ix+1);const yy=yToPix(v);x.fillStyle=r.discarded?'#ff6b6b':'#3ecf8e';x.beginPath();x.arc(xx,yy,3,0,Math.PI*2);x.fill();});}async function fetchStatus(){try{const r=await fetch('/api/status');const s=await r.json();document.getElementById('freq').textContent=fmt(s.current_frequency);document.getElementById('vdc').textContent=fmt(s.current_vdc);document.getElementById('vac').textContent=fmt(s.current_vac);document.getElementById('wait').textContent=fmt(s.wait_message);document.getElementById('repetition').textContent=s.running&&s.current_repeat&&s.total_repeats?String(s.current_repeat)+'/'+String(s.total_repeats):'-';document.getElementById('status').textContent=fmt(s.status);document.getElementById('running').innerHTML=s.running?'<span class="ok">EM EXECUCAO</span>':'<span class="hl">PARADO</span>';document.getElementById('pvdc').textContent=Number(s.programmed_vdc||0).toFixed(4)+' V';document.getElementById('pvac').textContent=Number(s.programmed_vac||0).toFixed(4)+' V';document.getElementById('nstd').textContent=fmt(s.n_std);document.getElementById('ndut').textContent=fmt(s.n_dut);const cf=(s.current_frequency||'').split(' ')[0];document.getElementById('freq_list').innerHTML=(s.programmed_frequencies_mhz||[]).map(f=>{const l=Number(f).toFixed(0);return(String(Number(f).toFixed(0))===cf)?'<span class="hl">> '+l+' <</span>':l}).join('<br>')||'-';document.getElementById('cycles').innerHTML=(s.cycle_rows||[]).map(c=>row([fmt(c.cycle),c.std===null?'-':Number(c.std).toLocaleString('pt-BR',{minimumFractionDigits:6,maximumFractionDigits:6}),c.dut===null?'-':Number(c.dut).toLocaleString('pt-BR',{minimumFractionDigits:6,maximumFractionDigits:6})])).join('');document.getElementById('results').innerHTML=(s.results_rows||[]).map(rw=>row([Number(rw.dif).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2}),Number(rw.delta).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2}),rw.discarded?'<span class="bad">DESCARTADO</span>':'<span class="ok">ACEITO</span>'])).join('');document.getElementById('summary').innerHTML=(s.summary_rows||[]).map(rw=>row([Number(rw.frequency_mhz).toFixed(0),Number(rw.mean).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2}),Number(rw.std).toLocaleString('pt-BR',{minimumFractionDigits:2,maximumFractionDigits:2})])).join('')||row(['-','-','-']);drawTrend(s.results_rows||[], Number(s.total_repeats||0));}catch(e){document.getElementById('status').textContent='Falha de comunicação com backend'}}
async function quick(cmd){if(cmd==='status'){await fetchStatus();return}if(cmd==='help'){document.getElementById('help').textContent='Comandos: start, stop, status, help, quit';return}if(cmd==='quit'){document.getElementById('help').textContent='No frontend web, use stop e feche a aba.';return}const r=await fetch('/api/'+cmd,{method:'POST'});const j=await r.json();document.getElementById('status').textContent=j.message||'OK';await fetchStatus()}
async function sendCmd(){const el=document.getElementById('cmd');const cmd=(el.value||'').trim().toLowerCase();el.value='';if(!cmd)return;await quick(cmd)}
async function loadConfig(){const r=await fetch('/api/config');const c=await r.json();const set=(id,v)=>document.getElementById(id).value=(v===undefined||v===null)?'':v;set('cfg_std',c.std_model);set('cfg_dut',c.dut_model);set('cfg_gpib_std',c.gpib_std);set('cfg_gpib_dut',c.gpib_dut);set('cfg_source_mode',c.source_mode);set('cfg_ac_model',c.ac_source_model);set('cfg_rf_model',c.rf_source_model);set('cfg_gpib_ac',c.gpib_ac_source);set('cfg_gpib_rf',c.gpib_rf_source);set('cfg_voltage',c.voltage);set('cfg_freq',c.frequency);set('cfg_rdut',c.r_dut);set('cfg_rstd',c.r_std);set('cfg_rep',c.repeticoes);set('cfg_wait',c.wait_time);set('cfg_heat',c.aquecimento);set('cfg_delta',c.delta_max_ppm);set('cfg_cycle',c.measurement_cycle);set('cfg_obs',c.observacoes);set('cfg_bme',String(c.use_bme280));set('cfg_tvc_std_model',c.tvc_std_model);set('cfg_tvc_std_serial',c.tvc_std_serial);set('cfg_tvc_dut_model',c.tvc_dut_model);set('cfg_tvc_dut_serial',c.tvc_dut_serial)}
async function saveConfig(){const g=id=>document.getElementById(id).value;const p={std_model:g('cfg_std'),dut_model:g('cfg_dut'),gpib_std:g('cfg_gpib_std'),gpib_dut:g('cfg_gpib_dut'),source_mode:g('cfg_source_mode'),ac_source_model:g('cfg_ac_model'),rf_source_model:g('cfg_rf_model'),gpib_ac_source:g('cfg_gpib_ac'),gpib_rf_source:g('cfg_gpib_rf'),voltage:g('cfg_voltage'),frequency:g('cfg_freq'),r_dut:g('cfg_rdut'),r_std:g('cfg_rstd'),repeticoes:g('cfg_rep'),wait_time:g('cfg_wait'),aquecimento:g('cfg_heat'),delta_max_ppm:g('cfg_delta'),measurement_cycle:g('cfg_cycle'),observacoes:g('cfg_obs'),use_bme280:g('cfg_bme'),tvc_std_model:g('cfg_tvc_std_model'),tvc_std_serial:g('cfg_tvc_std_serial'),tvc_dut_model:g('cfg_tvc_dut_model'),tvc_dut_serial:g('cfg_tvc_dut_serial')};const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});const j=await r.json();document.getElementById('status').textContent=j.message||'OK';await fetchStatus();}
document.getElementById('cmd').addEventListener('keydown',async(e)=>{if(e.key==='Enter')await sendCmd()});const ro=new ResizeObserver(()=>fitCanvas());ro.observe(document.getElementById('trend').parentElement);fetchStatus();loadConfig();setInterval(fetchStatus,700);
</script></body></html>"""

    @app.get('/api/status')
    def api_status():
        resp = requests.get(server_url + '/status', timeout=4, headers=auth_headers())
        return jsonify(resp.json()), resp.status_code

    @app.post('/api/start')
    def api_start():
        resp = requests.post(server_url + '/start', timeout=4, headers=auth_headers())
        return jsonify(resp.json()), resp.status_code

    @app.post('/api/stop')
    def api_stop():
        resp = requests.post(server_url + '/stop', timeout=4, headers=auth_headers())
        return jsonify(resp.json()), resp.status_code

    @app.get('/api/commands')
    def api_commands():
        resp = requests.get(server_url + '/commands', timeout=4, headers=auth_headers())
        return jsonify(resp.json()), resp.status_code

    @app.get('/api/config')
    def api_config_get():
        resp = requests.get(server_url + '/config', timeout=4, headers=auth_headers())
        return jsonify(resp.json()), resp.status_code

    @app.post('/api/config')
    def api_config_post():
        resp = requests.post(server_url + '/config', timeout=4, headers=auth_headers(), json=request.get_json(silent=True) or {})
        return jsonify(resp.json()), resp.status_code

    return app


def run_backend(host, port):
    global ui
    if ui is None:
        ui = MeasurementUI(enable_live=False)
        ui.set_status('Backend pronto. Aguardando comando start')
        ui.set_program([float(v.strip()) for v in freq_array], vdc_nominal, vac_nominal)
        ui.set_repetition(0, repeticoes)
    app = create_backend_app()
    app.run(host=host, port=port)


def run_web_client(server_url, host, port, token=''):
    app = create_web_client_app(server_url, token)
    app.run(host=host, port=port)


def run_tui_client(server_url, token=''):
    headers = {'X-Auth-Token': token} if token else {}
    remote_ui = MeasurementUI(enable_live=True)
    remote_ui.start()
    remote_ui.set_status('Conectando ao backend...')

    stop_client = threading.Event()

    def poll_status():
        while not stop_client.is_set():
            try:
                resp = requests.get(server_url + '/status', timeout=2, headers=headers)
                if resp.ok:
                    remote_ui.load_dict(resp.json())
            except Exception:
                remote_ui.set_status('Falha de comunicação com backend')
            time.sleep(0.5)

    poll_thread = threading.Thread(target=poll_status, daemon=True)
    poll_thread.start()

    def execute_command(cmd):
        cmd = cmd.strip().lower()
        if not cmd:
            return False
        if cmd == 'quit':
            return True
        if cmd == 'help':
            remote_ui.set_status('Comandos: start, stop, status, help, quit')
            return False
        if cmd in ('start', 'stop'):
            try:
                resp = requests.post(server_url + '/' + cmd, timeout=4, headers=headers)
                msg = resp.json().get('message', 'OK')
                remote_ui.set_status(msg)
            except Exception:
                remote_ui.set_status('Erro ao enviar comando {}'.format(cmd))
            return False
        if cmd == 'status':
            try:
                resp = requests.get(server_url + '/status', timeout=4, headers=headers)
                if resp.ok:
                    remote_ui.load_dict(resp.json())
            except Exception:
                remote_ui.set_status('Erro ao consultar status')
            return False
        remote_ui.set_status('Comando inválido. Use help')
        return False

    old_settings = None
    try:
        if sys.stdin.isatty():
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            buffer = ''
            remote_ui.set_command_input(buffer)
            quit_requested = False
            while not quit_requested:
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if ready:
                    ch = sys.stdin.read(1)
                    if ch in ('\n', '\r'):
                        remote_ui.set_command_input(buffer)
                        quit_requested = execute_command(buffer)
                        buffer = ''
                        remote_ui.set_command_input(buffer)
                    elif ch in ('\x7f', '\b'):
                        buffer = buffer[:-1]
                        remote_ui.set_command_input(buffer)
                    elif ch == '\x03':
                        quit_requested = True
                    elif ch.isprintable():
                        buffer += ch
                        remote_ui.set_command_input(buffer)
        else:
            while True:
                cmd = console.input('comando > ').strip().lower()
                remote_ui.set_command_input(cmd)
                if execute_command(cmd):
                    break
    finally:
        try:
            if old_settings is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        except Exception:
            pass
        stop_client.set()
        remote_ui.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['local', 'backend', 'tui', 'web'], default='local')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--server', default='http://127.0.0.1:8000')
    parser.add_argument('--token', default='')
    args = parser.parse_args()

    if args.mode == 'backend':
        run_backend(args.host, args.port)
    elif args.mode == 'tui':
        run_tui_client(args.server.rstrip('/'), args.token)
    elif args.mode == 'web':
        run_web_client(args.server.rstrip('/'), args.host, args.port, args.token)
    else:
        run_measurement_loop(enable_live=True)


if __name__ == '__main__':
    main()
