import math
import matplotlib.pyplot as plt
import numpy as np
import random
from scipy.signal import butter, sosfilt, sosfreqz, lfilter
from scipy.signal import spectrogram
import CLASIFICADOR_vgg16 as CLASIFICADOR_vgg16

import pipeline_v5.utils as utils
import torch



def filterces(b,_input):
    nb=len(b)
    N=len(_input)
    output=[]
    output = [0.0 for i in range(N+nb-1)]
    tmp = [0.0 for i in range(nb-1)]
    #input = np.concatenate((np.flipud(input[0:nb-1]), input))
    _input = np.concatenate((tmp, _input))
    for i in range (N):
        sum = 0.0
        for j in range (nb):
            sum = sum + b[nb-j-1]*_input[i+j]
        output[i]=sum
    output=output[0:N]    
    return output

def butter_bandpass(lowcut, highcut, fs, order=10):
  nyq = 0.5*fs
  low = lowcut/nyq
  high = highcut/nyq
  sos = butter(order, [low, high], analog=False, btype='band', output='sos')
  return sos

def butter_bandpass_filter(data, lowcut, highcut, fs, order=10):
  sos = butter_bandpass(lowcut,highcut,fs,order=order)  
  y = sosfilt(sos,data)
  return y

def sos_filter(sos,data):  
  y = sosfilt(sos,data)
  return y

def gapreduce(signal):
  x = signal.tolist()
  # this is the correct value
  # -2147483648
  MIN = -1*(pow(2,31))
  noise = 10*(random.uniform(0,1)) - 5
  for index in range(len(x)):
    if x[index] == MIN:
      #print("GAP FOUND")
      if (x[index+1] != MIN):
        # Only one point GAP
        x[index] = x[index+1] + noise
      else:
        # Multiple point GAP, Make a line
        x1 = index-1
        y1 = x[index-1]
        i = index+1
        while(x[i] == MIN):
          i+=1;
        x2 = i
        y2 = x[i]
        m = (y2-y1)/(x2-x1)
        c = y1 - (m*x1)
        for j in range(index,x2):
          # is necessary random valour for each point
          noise = 10*(random.uniform(0,1)) - 5
          x[j] = (m*j) + c + noise
    else:
      continue
#336027688A  
  return x

def detection(filtered_data,reduced_data,umbral):
  coeff = np.genfromtxt('b.txt')
  datalogical = []
  for i in range(len(filtered_data)):
    if (abs(filtered_data[i]) > umbral):
      datalogical.append(1)
    else:
      datalogical.append(0)
  
  np.concatenate((np.zeros((200), dtype=float), datalogical))
  tmp = np.concatenate((np.flipud(datalogical[0:500]), datalogical))
  #tmp = [0.0 for i in range(500)]    
  #tmp = np.concatenate((tmp, datalogical))

 
  print(len(tmp))

  yfil = lfilter(coeff, 1.0, tmp)
  yfil = yfil [500:]
#  yfilces = filterces(coeff,tmp)
#  yfilces = yfilces[500:]
#  print(yfilces[0:30])

  z = []
  for j in range(len(yfil)):
    if (abs(yfil[j]) > 0.1):
      z.append(1)
    else:
      z.append(0)
  # check init and end
  if z[0]== 1:
      z[0]=0
  if z[len(z)-1] == 1:
      z[len(z)-1]=0
  #FOR ADDING START AND END VALUES
  start = []
  end = []

  for k in range(len(z)-1):
    if (z[k] == 0):
      if (z[k+1] == 1):
        # Changing from 0 to 1 => START")
        #start.append(filtered_data[k])
        start.append(k)
    else:
      if (z[k+1] == 0):
        # Changing from 1 to 0 => END"
        #end.append(filtered_data[k])
        end.append(k)
  if len(start)>len(end):
    start=start[0:len(end)]
  if len(end)>len(start):
    end=end[0:len(start)]
  ev = np.column_stack((start,end))
 
  # VERIFYING THAT LMIN = 500 is maintained
  todel = []
  LMIN = 500
  for a in range(len(start)):
    #EVENT WIDTH HAS TO BE > LMIN
    if (abs(ev[a][0]-ev[a][1]) < LMIN):
      todel.append(a) #Stores the indexes of rows with width <LMIN which are to be deleted later
 
  ev_del = np.delete(ev,todel,axis=0) #Removes these events with < 500 width


  # VERIFYING THAT TWO EVENTS ARE NOT TOO CLOSE TOGETHER


  modify_idx = [] #To store index of all events whose END value is to be modified
  modify_values = [] #To store the new END value
  todel.clear() #To store the index of all rows to be removed
  flag = 0
  for b in range(np.shape(ev_del)[0]):
    if (flag == 0 and b != np.shape(ev_del)[0] - 1):
      if (abs(ev_del[b][1]-ev_del[b+1][0]) < 500): #Checking difference b/w End of first event and start of next event
        todel.append(b+1)
        modify_idx.append(b)
        try:
          if (abs(ev_del[b+1][1]-ev_del[b+2][0]) < 500): #Checking to see if difference b/w next 2 events is also short          
            todel.append(b+2)
            modify_values.append(ev_del[b+2][1]) 
            flag = 2
          else:
            modify_values.append(ev_del[b+1][1])
            flag = 1
        except:   
          modify_values.append(ev_del[b+1][1])
          flag = 1
    else:
      while(flag != 0):
        flag -= 1
        continue

  #TO GET THE FINAL EV LIST    
  ev_final = ev_del
  for c in range(len(modify_idx)):
    ev_final[modify_idx[c]][1] = modify_values[c] #Updating END values based on modify_values and modify_idx
  ev_final = np.delete(ev_del,todel,axis=0) 

  for i in range (len(ev_final)):
    # i shift 4 seconds 
    ev_final[i,0] = ev_final[i,0] -400
    if ev_final[i,0] < 0:
      ev_final[i,0] = 0
    #plt.plot(filtered_data[ev_final[i,0]:ev_final[i,1]])
    #plt.show()  

  return ev_final

"""CLASSIFICATION

"""

# Function for calculating decimal value of the binary sequence
def value(seq):
  total = 0
  exp = 0
  seq_flipped = np.flip(seq,1)[0]
  for bin in seq_flipped:
    total = total + bin*(2**exp)
    exp += 1
  return total
# Function for calculating first zero in the binary sequence
def valuetr(seq):
  total = 0
  exp = 0
  flag = 0
  cont = 10
  seq_flipped = np.flip(seq,1)[0]
  for bin in seq_flipped:
    if flag == 0: # find first zero in the sequence
        if bin == 0 :
            flag = 1
        else :
            cont = exp
            total = total + bin*(2**exp)
            exp += 1
  return total,cont

def check_TR(cd1,cd2,cd3,cd4,cd5):
  # we use the Least significant bit convention
  # i.e. 1000 is 1 not 8 ok?
  flag = 0
  #Checking if code01 is 1111xxxxxx ||  (1111000000 = 960) (1111111111 = 1023)
  #Checking if code01 is 1111xxxxxx = 7 ||  (X111000000 = 15) 
  if (not ((value(cd1)>7 and value(cd1)<15) or (value(cd1)>=30)  )):
    flag = 1

  #Checking if code02 is 0000xxxxxx || (0000111111 = 63)
  # code2 is 0000?
  if (value(cd2)>0): 
    flag = 1
  #Checking if first element is 0
  if (cd3[0][0] != 0):
    flag = 1
  if (cd4[0][0] != 0):
    flag = 1
  if (cd5[0][0] != 0):
    flag = 1
  
  if (flag == 1):
    #print("Not TR")
    return False
  else:
    #print("TR")
    return True

def check_LP(cd1,cd2,cd3,cd4,cd5):
  flag = 0
  #code01 = 1110xxxxxx || (1110111111 = 959) and (1110000000 = 896)
  #code01 = 1110xxxxxx || (1110111111 = 1015) and (1110000000 = 7)
  if (not (value(cd1) <= 1015 and value(cd1) >= 7)): 
    flag = 1
  # code02=1110xxxxxx or 1100xxxxxx or 1000xxxxxx||(1110000000=896)(1110111111=959)|(1100000000=768)(1100111111=831)|(1000111111=575)(1000000000=512)
  # code02=1110xxxxxx or 1100xxxxxx or 1000xxxxxx||(1110000000=7)(1110111111=1015)|(1100000000=3)(1100111111=1011)|(1000111111=1009)(1000000000=1)
  # if (not ((value(cd2)>=7 and value(cd2)<=1015)or (value(cd2)>=3 and value(cd2)<=1011)or (value(cd2)>=1 and value(cd2)<=1009))):
  if (not ((value(cd2)>=3 and value(cd2)>0))) :#or (value(cd2)>=3 and value(cd2)<=1011)or (value(cd2)>=1 and value(cd2)<=1009))):
    flag = 1
   #code03 < 4
  if (value(cd3) < 4):
    flag = 1
  #Checking if first element is 0
  if (cd4[0][0] != 0):
    flag = 1
  if (cd5[0][0] != 0):
    flag = 1

  if (flag == 1):
    #print("Not LP")
    return False
  else:
    #print("LP")
    return True

def check_VT(cd1,cd2,cd3,cd4,cd5):
  flag = 0
  #code01 = 1100xxxxxx||(1100000000=768)(1100111111=831)
  if (not (value(cd1) <= 3) and (value(cd1) > 0)):
  #code01 = 1100xxxxxx||(1100000000=3)(1100111111=831)
  #if (not (value(cd1) <= 3)):
    flag = 1
  #code02 = 1000xxxxxx or 1100xxxxxx ||(1000000000=512)(1000111111=575)|(1100000000=768)(1100111111=831)
  #if (not ((value(cd2) >=768 and value(cd2) <= 831)or (value(cd2) >=512 and value(cd2) <=575))):
  #code02 = 1000xxxxxx or 1100xxxxxx ||(1000000000=512)(1000111111=575)|(1100000000=768)(1100111111=831)
  if (not ((value(cd2) <=1) and (value(cd2)>0))):
    flag = 1
  #code03 = 1000xxxxxx or 1100xxxxxx ||(1000000000=512)(1000111111=575)|(1100000000=768)(1100111111=831)
  if (not ((value(cd3) >=1 ) and (value(cd3) > 0 ))): #or (value(cd3) >=512 and value(cd3) <=575))):
    flag = 1
  #code04 = 10xxxxxxxx||(1011111111 = 767)(1000000000 = 512)
  if (not ((value(cd4) <= 3) and (value(cd4) > 0))):
    flag = 1
  if (flag == 0):  #All other conditions are true
    #code05 = 00xxxxxxxx     (0011111111 = 255)
    if (value(cd5) < 1):
      flag = 1
  else: #All other conditions are NOT true
    #code05 = 10xxxxxxxx||(1011111111 = 767)(1000000000 = 512)
    if (not ((value(cd5) >= 1 and value(cd5) < 0))):
      flag = 1

  if (flag == 1):
    #print("Not VT")
    return False
  else:
    #print("VT")
    return True

def check_class(cd1,cd2,cd3,cd4,cd5,decay,decay2,envolvente,code01tmp,cd1vt):
  clase = 4
  code01 = value(cd1)
  code02 = value(cd2)
  code03 = value(cd3)
  code04 = value(cd4)
  code05 = value(cd5)
  code01vt = cd1vt
  code01tr,conttr = valuetr(cd1)
  
  if code02 > 0  and (not(np.fix(code02/2) == code02/2)) :

    if code02 > 0 and (np.fix(code01/2) == code01/2) :
      clase = 4 # AV o TC

    else : #posible VT
        if code02 == 1 and code01vt > 0:
            clase = 1
            
        if code02 == 3 and code01vt == 3 and  decay > 88:
            clase = 1
        else :            
            clase = 2
        if code02 == 3 and code01vt == 7 and  decay < 60:
            clase = 1
        else :
            clase = 2
                    
        if code01 == 3 and code02 == 3:
            clase = 2 # LP

        if code01 == 3 and code02 == 1:
            clase = 2 # LP 
        
        if code01 == 7 and code02 == 3  and code03 == 3:
            clase = 2 # Lp clasico
        
        if code01 == 7 and code02 == 7  and code03 == 3:
            clase = 2 # Lp clasico
        
        if code02 == 3 and code01tmp == 3 and code03 == 3 :
            clase = 2 # LP clasico
            
        if code02 == 3 and code01tmp == 3 and code03 == 1 :
            clase = 2 # LP clasico

        if code02 == 3 and  (np.fix(code01tmp/2) == code01tmp/2) and code03 > 0 :
            clase = 5 # AV
        
        # VTs
        if code01 == 1 and code02 == 1 :
            clase = 1 # VT

        if code01 == 1 and code02 == 1 and code03 == 1 :
            clase = 1 # VT        
        if code01 == 3 and code02 == 1 and code03 == 1 :
            clase = 1 # VT

        if code01 == 3 and code02 == 3 and code03 == 1 :
            clase = 1 # VT
            
        if code01 == 1 and code02 > 3 and code03 == 1 :
            clase = 1 # VT

        if code01 == 1 and code02 +code03 > 2 and code04 == 1 :
            clase = 1 # VT
      
        if code01 == 3 and code02 == 3 and code03 == 3 :
            clase = 1 # VT
      
       
########################################################      
      
  else: # is LP o TR 
    if code01 > 994 :
      #clase = 3 # TR
        if code01tr > 3 :
            clase = 3
        else:
            clase = 4
      
    else:
      if code01 == 3 :
        clase = 2 # LP
      elif code01 == 7:
        clase = 2 # LP
      elif code01 == 15 :
        if decay < 50: 
          clase = 2 # LP
        else:
          clase = 3 # TR
      elif code01 == 31:
        clase = 3 # TR
      elif code01 == 255 :
        clase = 3 # TR        
      else:
        if (envolvente < 0.5):
          clase = 2 # LP
        else:
          clase = 3 # TR
  return clase            

    
def classification(filtered_data, filtered_data2, ev_final, model=None, ood_detector=None):
    """
    Clasifica los eventos detectados usando VGG16 + reglas espectrales.

    Args:
        model: Modelo VGG16 pre-cargado (opcional). Si None, se carga desde disco.
               Pasar el modelo pre-cargado mejora el rendimiento en producción
               (evita recargar VGG16 en cada llamada).
        ood_detector: Detector OOD pre-cargado (opcional). Si None, se carga desde disco.

    Returns:
        tpclass: clases VGG16 (1=VT, 2=LP, 3=TR, 4=OT)
        tpclass2: clases por reglas espectrales
        prob_matrix: matriz (ne, 4) con [prob_lp, prob_tr, prob_vt, prob_ot]
    """
    tpclass = []
    tpclass2 = []
    ne,nf = np.shape(ev_final)

    # Cargar modelos solo si no fueron provistos (fallback para uso standalone)
    if model is None:
        weights_path = "pipeline_v5/rep2_weights.pt"
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = utils.load_model(weights_path, device)
    if ood_detector is None:
        OOD_path = "pipeline_v5/OOD_detector.pkl"
        ood_detector = utils.load_OOD_detector(OOD_path)

    OOD_detector = ood_detector

    # Matriz de probabilidades acumulada: columnas [prob_lp, prob_tr, prob_vt, prob_ot]
    prob_matrix = np.zeros((ne, 4))

    for i_ev in range (0,ne):
        # clasificador pipeline
        input_signal=filtered_data[ev_final[i_ev,0]:ev_final[i_ev,1]]
        ev1 = [[0, len(input_signal)]]

        # tpclass0 = CLASIFICADOR_vgg16.clasificar(input_signal, ev1, model)
        # 2.- Clasificacion: "VT": 1.0, "LP": 2.0, "TR": 3.0, "OT": 4.0
        tpclass0, ev_probs = utils.predict(input_signal, ev1, model, OOD_detector, return_probs=True)
        prob_matrix[i_ev] = ev_probs[0]

        # decay calculus, if >50% is TR, if < 50 is LP
        lp1 = filtered_data[ev_final[i_ev,0]:ev_final[i_ev,1]]
        tmplp1=np.asarray(0.5*max(lp1))
        tmplp1=lp1>tmplp1
        tmplp1 = np.cumsum(lp1*tmplp1)
        tmp1 = max(tmplp1)
        tmp2 = np.argmax(tmplp1)
        decay = 100 * tmp2/len(lp1)
        signal2 = filtered_data2[ev_final[i_ev,0]:ev_final[i_ev,0]+9000]      

        if (len(signal2))<9000:
          signal2 = np.concatenate((signal2,signal2[len(signal2)-1]*np.ones(9000-len(signal2))))
        signal2 = np.asarray(signal2)

        f, t, Sxx = spectrogram(signal2,fs=100,window='hamming',nperseg=200,noverlap=172,nfft=1000,mode='complex')

        temp = Sxx[0:250,0:250]
        product = temp*np.conj(temp)
        #plt.pcolormesh(product.real, shading='gouraud',cmap="jet")
        #plt.ylabel('Frequency [Hz]')
        #plt.xlabel('Time [sec]')
        #plt.show()


        temp = np.log10(100*product.real+1e-5)

        #plt.pcolormesh(temp, shading='gouraud',cmap="jet")
        #plt.ylabel('Frequency [Hz]')
        #plt.xlabel('Time [sec]')
        #plt.show()

        temp = temp/temp.max()

        #temp = (temp > 0.7)
        # for diferences with spectrogram calculus must be change threshold
        temp = (temp > 0.76)
        temp = np.flipud(temp)

        """CALCULATING SUB MATRIX SUM"""

        #B = np.ndarray((10,5))
        B = np.ndarray((5,10))
        count_i = 0
        for i in range(0,250,50):
          count_j = 0
          for j in range(0,250,25):
            sub_matrix_sum = 0
            for k in range(i,i+50):
              for l in range(j,j+25):
                sub_matrix_sum += temp[k][l]
            B[count_i][count_j] = sub_matrix_sum
            count_j += 1
          count_i += 1
        B.shape
        final = (B > 100).astype(int)

        """# CLASSIFICATION CRITERIA"""
        final2 = final
        code1 = final2[4:5,:10]
        if code1[0,4] == 0 :
            code01vt=code1[0:2]
            code01vt=value(np.fliplr(code01vt))
        else :
            code01vt = 0
                
       
        code1 = final2[4:5,:10]
        code1 = np.fliplr(code1)
        code2 = final2[3:4,0:4]
        code2 = np.fliplr(code2)
        code3 = final2[2:3,0:3]
        code3 = np.fliplr(code3)
        code4 = final2[1:2,0:3]
        code4 = np.fliplr(code4)
        code5 = final2[:1,0:3]
        code5 = np.fliplr(code5)

        #print(code2)
        #print(code1)
        # decay2 calculus
        tmp=[]
        for i in range (1,11):
          tmp.append((value(code1)+1)/pow(2,i))
        
        comparison=[]
        tmp2 = 1
        zerotrue = 1
        for i in range (0,10):
          if tmp[i] == np.fix(tmp[i]):
            comparison.append(1)
            tmp2 = tmp2 + zerotrue
          else:
            comparison.append(0)
            zerotrue = 0       
        if sum(comparison) < 10:                  
          decay2 = 10 * tmp2
          code01tmp = tmp2 - 1
        else:
          decay2 = 100
          code01tmp = 10
          
        lp2 = filtered_data[ev_final[i_ev,0]:int(ev_final[i_ev,0] + np.fix(decay2/100*9000))]

        tmplp2=np.asarray(0.5*max(lp2))
        tmplp2=lp2>tmplp2
        tmplp2 = np.cumsum(lp2*tmplp2)
        tmp1 = max(tmplp2)
        tmp2 = np.argmax(tmplp2)
        decay2 = 100 * tmp2/len(lp2)                         

        # envelopment calculus
        ddata = filtered_data[ev_final[i_ev,0]:ev_final[i_ev,1]]
        cums = np.cumsum(abs(ddata))
        c=np.transpose(np.arange(1,len(cums)+1))
        cums = cums / c
        tmp0 = max(cums)
        tmp1 = np.argmax(cums)
        fcums = np.flipud(cums)
        to = np.where(fcums>0.707*tmp0)
        to=np.asarray(to)
        envolvente = (len(fcums)-to[0,0])/(ev_final[i_ev,1]-ev_final[i_ev,0])
        largo = 9*np.log2(value(code1)+1)

        
        evclass=4
        if (not (check_TR(code1,code2,code3,code4,code5))):
          if (not (check_LP(code1,code2,code3,code4,code5))):
            if (not (check_VT(code1,code2,code3,code4,code5))):
              #print("No Class Identified")
              evclass=4
            else:
              #print("VT Event")
              evclass=1
          else:
            #print("LP Event")
            evclass=2
        else:
          #print("TR Event")
          evclass=3
        # tpclass.append(evclass)
        tpclass.append(tpclass0[0])
        tpclass2.append(check_class(code1,code2,code3,code4,code5,decay,decay2,envolvente,code01tmp,code01vt))

    return tpclass, tpclass2, prob_matrix
