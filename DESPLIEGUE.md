# Despliegue OVDAS-Core en ovdasserv — Documentación completa

> Fecha: 2026-05-25
> Servidor: `ovdasserv` — Ubuntu 20.04.6 LTS, x86_64, Intel i7-2600 @ 3.40GHz, 8 CPUs, 7.7GB RAM, 408GB disco
> IP LAN: `192.168.1.6` (SSH puerto 6022)
> IP pública: `200.13.0.53`
> Usuario: `ovdas` (sudo ALL, NOPASSWD configurado)

---

## Índice

1. [Contexto y restricciones](#1-contexto-y-restricciones)
2. [Arquitectura del sistema](#2-arquitectura-del-sistema)
3. [Lo que se instaló](#3-lo-que-se-instaló)
4. [Proceso de instalación paso a paso](#4-proceso-de-instalación-paso-a-paso)
5. [Estado actual del cluster](#5-estado-actual-del-cluster)
6. [Acceso a los servicios](#6-acceso-a-los-servicios)
7. [Token del Kubernetes Dashboard](#7-token-del-kubernetes-dashboard)
8. [Autoescalado HPA](#8-autoescalado-hpa)
9. [Docker Compose (modo desarrollo)](#9-docker-compose-modo-desarrollo)
10. [Comandos útiles de operación](#10-comandos-útiles-de-operación)
11. [Archivos importantes](#11-archivos-importantes)
12. [Notas y advertencias](#12-notas-y-advertencias)

---

## 1. Contexto y restricciones

El servidor **no tiene acceso a internet** y no debe tenerlo. Esto implica:

- No se puede usar `apt-get`, `pip install`, `docker pull`, ni `curl` hacia URLs externas directamente desde el servidor.
- Todo software debe descargarse en la **máquina local** (que sí tiene internet) y transferirse vía **SCP/rsync** sobre SSH.
- Los **Dockerfiles** usan `pip install` y `apt-get` durante el build → las imágenes se construyeron en la máquina local (donde `pip` puede alcanzar PyPI) y se transfirieron como tarballs comprimidos.

---

## 2. Arquitectura del sistema

```
┌─────────────────────────────────────────────────────────────┐
│                    ovdasserv (K3s node)                     │
│                                                             │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   │
│  │  wws-poller  │   │     core     │   │ api-gateway  │   │
│  │  (singleton) │   │  FastAPI     │   │  FastAPI     │   │
│  │  modo dummy  │   │  :30800      │   │  :30808      │   │
│  └──────┬───────┘   └──────┬───────┘   └──────────────┘   │
│         │ seismic-data-pvc │ RabbitMQ callbacks            │
│         ▼                  ▼                               │
│  ┌──────────────┐   ┌──────────────┐                       │
│  │   RabbitMQ   │   │  PostgreSQL  │                       │
│  │  :31672(UI)  │   │  schemas:    │                       │
│  │  4 queues    │   │  core/detec/ │                       │
│  └──────┬───────┘   │  picking/    │                       │
│         │           └──────────────┘                       │
│    ┌────┴──────────────────────────┐                       │
│    ▼            ▼                 ▼                        │
│  worker-     worker-         worker-                       │
│  deteccion   picking         localizacion                  │
│  (HPA 1-5)   (HPA 1-5)       (HPA 1-3)                    │
│  VGG16+ADOVE P-S picker      Hyposat                       │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │           Kubernetes Dashboard  :30900 (HTTPS)       │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### Colas RabbitMQ

| Cola | Consumidor |
|------|-----------|
| `ovdas.deteccion` | worker-deteccion |
| `ovdas.picking` | worker-picking |
| `ovdas.localizacion` | worker-localizacion |
| `ovdas.callbacks` | core (resultados) |

### Estados del pipeline

```
INGRESADO → DETECTANDO → PICKING → LOCALIZANDO → CLASIFICANDO → COMPLETADO
                                                               ↘ ERROR
```

---

## 3. Lo que se instaló

### En la máquina local (build)
- Se construyeron 6 imágenes Docker usando el código fuente en `/home/riley/Documents/ovdascla/ovdas-core/`
- Se descargaron binarios de K3s v1.31.6, Docker v26.1.4, docker-compose v2.27.0

### En el servidor remoto

| Componente | Versión | Método de instalación |
|---|---|---|
| Docker Engine | 26.1.4 | Binario estático (tarball), systemd service |
| docker-compose | 2.27.0 | Binario standalone en `/usr/local/bin/` |
| K3s (Kubernetes) | v1.31.6+k3s1 | Airgap install (sin internet) |
| kubectl | via k3s symlink | `/usr/local/bin/kubectl → k3s` |

### Imágenes disponibles en el servidor (Docker + K3s containerd)

| Imagen | Tamaño | Descripción |
|---|---|---|
| `ovdas/core:latest` | 213 MB | Core API + Saga orchestrator |
| `ovdas/api-gateway:latest` | 198 MB | API Gateway (composición) |
| `ovdas/worker-picking:latest` | 538 MB | P-S picker (ObsPy) |
| `ovdas/worker-localizacion:latest` | 177 MB | Localización Hyposat (Fortran) |
| `ovdas/wws-poller:latest` | 195 MB | Poller Winston Wave Server |
| `ovdas/worker-deteccion:latest` | **3.71 GB** | ADOVE + VGG16 + PyTorch CPU |
| `postgres:15-alpine` | 274 MB | Base de datos |
| `rabbitmq:3.12-management-alpine` | 173 MB | Message broker |
| `redis:7-alpine` | 39 MB | Cache |
| `busybox:1.36` | 4.4 MB | Init containers (netcat wait) |
| `kubernetesui/dashboard:v2.7.0` | ~238 MB | Kubernetes Dashboard |
| `kubernetesui/metrics-scraper:v1.0.8` | ~42 MB | Métricas para el dashboard |

---

## 4. Proceso de instalación paso a paso

### Fase 1 — Descarga en máquina local

```bash
mkdir -p /tmp/ovdas-install/app-images

# K3s
wget -O /tmp/ovdas-install/k3s \
  "https://github.com/k3s-io/k3s/releases/download/v1.31.6%2Bk3s1/k3s"
wget -O /tmp/ovdas-install/k3s-airgap-images-amd64.tar.zst \
  "https://github.com/k3s-io/k3s/releases/download/v1.31.6%2Bk3s1/k3s-airgap-images-amd64.tar.zst"
wget -O /tmp/ovdas-install/k3s-install.sh \
  "https://raw.githubusercontent.com/k3s-io/k3s/master/install.sh"

# Docker estático
wget -O /tmp/ovdas-install/docker-static.tgz \
  "https://download.docker.com/linux/static/stable/x86_64/docker-26.1.4.tgz"
wget -O /tmp/ovdas-install/docker-compose \
  "https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64"

# Imágenes de infraestructura
docker pull postgres:15-alpine rabbitmq:3.12-management-alpine redis:7-alpine busybox:1.36
docker save postgres:15-alpine rabbitmq:3.12-management-alpine redis:7-alpine busybox:1.36 \
  | gzip > /tmp/ovdas-install/infra-images.tar.gz

# Dashboard
wget -O /tmp/ovdas-install/dashboard.yaml \
  "https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml"
docker pull kubernetesui/dashboard:v2.7.0 kubernetesui/metrics-scraper:v1.0.8
docker save kubernetesui/dashboard:v2.7.0 kubernetesui/metrics-scraper:v1.0.8 \
  | gzip > /tmp/ovdas-install/dashboard-images.tar.gz
```

### Fase 2 — Build de imágenes de aplicación (local, con internet)

```bash
cd /home/riley/Documents/ovdascla/ovdas-core

# 5 servicios en paralelo
docker build -t ovdas/core:latest              ./core           &
docker build -t ovdas/api-gateway:latest       ./api-gateway    &
docker build -t ovdas/worker-picking:latest    ./worker-picking &
docker build -t ovdas/worker-localizacion:latest ./worker-localizacion &
docker build -t ovdas/wws-poller:latest        ./wws-poller     &
wait

# worker-deteccion por separado (PyTorch CPU ~800MB de descarga, ~20 min)
docker build -t ovdas/worker-deteccion:latest  ./worker-deteccion

# Guardar como tarballs
for svc in core api-gateway worker-picking worker-localizacion wws-poller worker-deteccion; do
  docker save ovdas/${svc}:latest | gzip > /tmp/ovdas-install/app-images/${svc}.tar.gz
done
```

### Fase 3 — Transferencia al servidor (via SSH ControlMaster)

```bash
SCP="scp -o ControlPath=/tmp/ssh-ovdas/ctl -P 6022"

# Setup files
$SCP /tmp/ovdas-install/k3s /tmp/ovdas-install/k3s-install.sh \
     /tmp/ovdas-install/k3s-airgap-images-amd64.tar.zst \
     /tmp/ovdas-install/docker-static.tgz /tmp/ovdas-install/docker-compose \
     /tmp/ovdas-install/dashboard.yaml \
     ovdas@192.168.1.6:/tmp/ovdas-install/

# Imágenes
$SCP /tmp/ovdas-install/infra-images.tar.gz \
     /tmp/ovdas-install/dashboard-images.tar.gz \
     ovdas@192.168.1.6:/tmp/ovdas-install/

# Apps (worker-deteccion ~2GB, usa rsync --partial para reanudar si falla)
rsync --partial --progress \
  -e "ssh -o ControlPath=/tmp/ssh-ovdas/ctl -p 6022" \
  /tmp/ovdas-install/app-images/*.tar.gz \
  ovdas@192.168.1.6:/tmp/ovdas-install/app-images/
```

### Fase 4 — Instalación de Docker en el servidor

```bash
# Extraer e instalar binarios
tar xzf /tmp/ovdas-install/docker-static.tgz -C /tmp/ovdas-install/
sudo cp /tmp/ovdas-install/docker/* /usr/local/bin/
sudo cp /tmp/ovdas-install/docker-compose /usr/local/bin/docker-compose

# Grupo y permisos
sudo groupadd docker
sudo usermod -aG docker ovdas

# Servicio systemd
sudo tee /etc/systemd/system/docker.service > /dev/null << 'EOF'
[Unit]
Description=Docker Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/dockerd
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
LimitNOFILE=infinity
LimitNPROC=infinity

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now docker
```

> **Nota sobre sudo en pipelines:** Al usar `sudo` dentro de pipes (e.g. `gunzip | sudo docker load`),
> sudo no puede leer la contraseña de stdin porque ya está ocupado por el pipe.
> Se resolvió configurando NOPASSWD:
> ```bash
> echo 'ovdas ALL=(ALL) NOPASSWD: ALL' > /tmp/ovdas-nopasswd
> sudo cp /tmp/ovdas-nopasswd /etc/sudoers.d/ovdas-nopasswd
> sudo chmod 440 /etc/sudoers.d/ovdas-nopasswd
> ```

### Fase 5 — Carga de imágenes en Docker

```bash
# Cargar todas las imágenes en el Docker daemon del servidor
gunzip -c /tmp/ovdas-install/infra-images.tar.gz | sudo docker load
for f in /tmp/ovdas-install/app-images/*.tar.gz; do
  gunzip -c "$f" | sudo docker load
done
```

### Fase 6 — Instalación de K3s (airgap)

```bash
# Imágenes internas de K3s (coredns, metrics-server, local-path-provisioner)
sudo mkdir -p /var/lib/rancher/k3s/agent/images/
sudo cp /tmp/ovdas-install/k3s-airgap-images-amd64.tar.zst \
        /var/lib/rancher/k3s/agent/images/

# Instalar binario y ejecutar script offline (sin Traefik)
sudo cp /tmp/ovdas-install/k3s /usr/local/bin/k3s
sudo chmod +x /usr/local/bin/k3s
INSTALL_K3S_SKIP_DOWNLOAD=true \
  bash /tmp/ovdas-install/k3s-install.sh --disable=traefik

# Configurar kubectl para el usuario ovdas
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown ovdas:ovdas ~/.kube/config
chmod 600 ~/.kube/config
echo 'export KUBECONFIG=~/.kube/config' >> ~/.bashrc
```

### Fase 7 — Importar imágenes en el containerd de K3s

> K3s tiene su propio containerd, **independiente del Docker daemon**.
> Las imágenes deben importarse en ambos por separado.

```bash
# Infraestructura + dashboard
gunzip -c /tmp/ovdas-install/infra-images.tar.gz | sudo k3s ctr images import -
gunzip -c /tmp/ovdas-install/dashboard-images.tar.gz | sudo k3s ctr images import -

# Aplicaciones
for f in /tmp/ovdas-install/app-images/*.tar.gz; do
  gunzip -c "$f" | sudo k3s ctr images import -
done
```

> **Problema encontrado:** El Dashboard tenía `imagePullPolicy: Always` (default) y K3s intentaba
> hacer pull desde Docker Hub aunque la imagen estuviera importada localmente. Se resolvió
> parcheando el deployment:
> ```bash
> sudo k3s kubectl -n kubernetes-dashboard patch deployment kubernetes-dashboard \
>   --type=json \
>   -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]'
> ```

### Fase 8 — Crear PVC faltante y desplegar manifiestos

Los manifiestos `09-worker-localizacion.yaml` y `10-wws-poller.yaml` referencian `seismic-data-pvc`
pero ningún archivo lo creaba. Se creó el manifiesto faltante:

```bash
cat > /ovdas/ovdasufro/ovdas-core/k8s/00-seismic-pvc.yaml << 'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: seismic-data-pvc
  namespace: ovdas
spec:
  accessModes:
    - ReadWriteOnce   # OK en single-node: todos los pods van al mismo nodo
  storageClassName: local-path
  resources:
    requests:
      storage: 20Gi
EOF
```

Despliegue de todos los manifiestos en orden:

```bash
cd /ovdas/ovdasufro/ovdas-core
for f in $(ls k8s/*.yaml | sort); do
  sudo k3s kubectl apply -f "$f"
done
```

### Fase 9 — Kubernetes Dashboard

```bash
# Aplicar manifiesto
sudo k3s kubectl apply -f /tmp/ovdas-install/dashboard.yaml

# Crear usuario admin con acceso cluster-admin
sudo k3s kubectl apply -f - << 'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: admin-user
  namespace: kubernetes-dashboard
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: admin-user
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: cluster-admin
subjects:
- kind: ServiceAccount
  name: admin-user
  namespace: kubernetes-dashboard
EOF

# Exponer via NodePort 30900 (HTTPS)
sudo k3s kubectl -n kubernetes-dashboard patch svc kubernetes-dashboard \
  -p '{"spec":{"type":"NodePort","ports":[{"port":443,"targetPort":8443,"nodePort":30900}]}}'

# Parchear imagePullPolicy (imagen ya importada localmente)
sudo k3s kubectl -n kubernetes-dashboard patch deployment kubernetes-dashboard \
  --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"}]'
```

---

## 5. Estado actual del cluster

```
$ kubectl -n ovdas get pods -o wide
NAME                                   READY   STATUS    RESTARTS
api-gateway-79c9cb89cf-phzkg           1/1     Running   0
core-797d78c8f8-5qhbw                  1/1     Running   0
postgres-0                             1/1     Running   0
rabbitmq-0                             1/1     Running   0
redis-66c6678858-zql9k                 1/1     Running   0
worker-deteccion-76f44cdd6f-pst95      1/1     Running   0
worker-localizacion-7cc4996f68-6brtk   1/1     Running   0
worker-picking-669f4fbc6d-xpjk6        1/1     Running   0
wws-poller-55c9b55d4c-52djm            1/1     Running   0
```

```
$ kubectl -n ovdas get hpa
NAME                      REFERENCE                        TARGETS        MINPODS   MAXPODS   REPLICAS
hpa-worker-deteccion      Deployment/worker-deteccion      cpu: XX%/60%   1         5         1
hpa-worker-localizacion   Deployment/worker-localizacion   cpu: XX%/70%   1         3         1
hpa-worker-picking        Deployment/worker-picking        cpu: XX%/60%   1         5         1
```

```
$ kubectl -n ovdas get svc
NAME          TYPE        PORT(S)
api-gateway   NodePort    8080:30808/TCP
core          NodePort    8000:30800/TCP
postgres      ClusterIP   5432/TCP
rabbitmq      ClusterIP   5672/TCP,15672/TCP
rabbitmq-ui   NodePort    15672:31672/TCP
redis         ClusterIP   6379/TCP
```

---

## 6. Acceso a los servicios

Todos los accesos son vía **SSH tunnel** desde tu máquina local.

```bash
# Todo en un solo comando (ejecutar en terminal separada con -N):
ssh -L 8000:192.168.1.6:30800 \
    -L 8080:192.168.1.6:30808 \
    -L 15672:192.168.1.6:31672 \
    -L 8443:192.168.1.6:30900 \
    -L 5050:192.168.1.6:30500 \
    -p 6022 -N ovdas@192.168.1.6
```

| Servicio | NodePort | URL local | Credenciales |
|---|---|---|---|
| Core API | 30800 | http://localhost:8000/docs | — |
| API Gateway | 30808 | http://localhost:8080/docs | — |
| RabbitMQ UI | 31672 | http://localhost:15672 | ovdas / ovdas |
| K8s Dashboard | 30900 | https://localhost:8443 | Token (ver sección 7) |
| pgAdmin | 30500 | http://localhost:5050 | admin@ovdas.cl / ovdas123 |

---

## 7. Token del Kubernetes Dashboard

Para hacer login en `https://localhost:8443` → seleccionar **Token** y pegar el siguiente:

```
eyJhbGciOiJSUzI1NiIsImtpZCI6ImRveG5FNWczbndTQkdRSDRsZWUyNkVTRkRrRkx1YkszR19UN1p0dFMyNEEifQ.eyJhdWQiOlsiaHR0cHM6Ly9rdWJlcm5ldGVzLmRlZmF1bHQuc3ZjLmNsdXN0ZXIubG9jYWwiLCJrM3MiXSwiZXhwIjo0OTMzMjc4NjkyLCJpYXQiOjE3Nzk2Nzg2OTIsImlzcyI6Imh0dHBzOi8va3ViZXJuZXRlcy5kZWZhdWx0LnN2Yy5jbHVzdGVyLmxvY2FsIiwianRpIjoiMDY0ODQwYTItNzA5Zi00ZGE5LWI3YjctMDRhMWFhMjhiNzEyIiwia3ViZXJuZXRlcy5pbyI6eyJuYW1lc3BhY2UiOiJrdWJlcm5ldGVzLWRhc2hib2FyZCIsInNlcnZpY2VhY2NvdW50Ijp7Im5hbWUiOiJhZG1pbi11c2VyIiwidWlkIjoiNGY1NGIwMWQtNDUwYi00N2E1LWEyODktZjRlNDIzM2EzNDRhIn19LCJuYmYiOjE3Nzk2Nzg2OTIsInN1YiI6InN5c3RlbTpzZXJ2aWNlYWNjb3VudDprdWJlcm5ldGVzLWRhc2hib2FyZDphZG1pbi11c2VyIn0.KXE407BTOHNoD_yjHpmiKINEkfiasLVRY2O36Ui7elhNDd5TI6bBHlCF9KjCaX4JVXYUWFezhwUbdBvbrdQdaQO-ulm9vurN3KfCfrh2g95LAjrBlpr7b_YI04WnZP4hTrcVaMN_45LLz4lfgWkTWu8_FJimo-McdXiZDunxuqZMYJ2mvmuT1FL-YeMBf0PxTCctEx0YL1hDvs3-ZyNWbxtiDhGJNOfZDpER1y2tbMswEqrDhiEMY3awjJ-jDFbrtmURsJvcuI8yr4ghWCwPyr_jbEen4PGlicGJtNyGYIsPntiZWK11IL1l6lxjv0LTZG4UPBvOGWa1RNdG1WdcBg
```

> El navegador mostrará advertencia de certificado SSL auto-firmado — hacer clic en "Avanzado" → "Continuar".

Para regenerar un nuevo token (en el servidor):
```bash
sudo k3s kubectl -n kubernetes-dashboard create token admin-user --duration=876000h
```

---

## 8. Autoescalado HPA

Los workers escalan automáticamente según CPU. El metrics-server viene incluido en el airgap bundle de K3s.

| Worker | Min | Max | Trigger CPU | Scale Up | Scale Down |
|---|---|---|---|---|---|
| worker-deteccion | 1 | 5 | > 60% | +2 pods / 15s | -1 pod / 60s |
| worker-picking | 1 | 5 | > 60% | +2 pods / 15s | -1 pod / 60s |
| worker-localizacion | 1 | 3 | > 70% | +1 pod / 30s | -1 pod / 60s |

Escalar manualmente:
```bash
# Forzar 3 replicas de worker-deteccion
sudo k3s kubectl -n ovdas scale deployment/worker-deteccion --replicas=3

# Ver estado del HPA en tiempo real
sudo k3s kubectl -n ovdas get hpa -w
```

---

## 9. pgAdmin — Visualizador de base de datos

### Acceso

Tunnel SSH desde tu máquina local:
```bash
ssh -L 5050:192.168.1.6:30500 -p 6022 -N ovdas@192.168.1.6
```

Abrir: `http://localhost:5050`

| Campo | Valor |
|---|---|
| Email | `admin@ovdas.cl` |
| Contraseña | `ovdas123` |

Al hacer login aparece el servidor **"OVDAS PostgreSQL"** ya pre-configurado en el panel izquierdo.
Para conectarse por primera vez, hacer clic derecho → **Connect** e ingresar la contraseña: `ovdas`

### Schemas disponibles

| Schema | Tablas principales |
|---|---|
| `core` | `pipeline_eventos`, `volcanes`, `estaciones` |
| `deteccion` | `resultados` (SNR, label VT/LP/TR, probabilidades) |
| `picking` | `resultados` (t_p, t_s, amplitud, freq_dom) |

### Cómo se desplegó

```bash
# 1. Imagen descargada localmente y transferida
docker pull dpage/pgadmin4:8.6
docker save dpage/pgadmin4:8.6 | gzip > pgadmin.tar.gz
scp -P 6022 pgadmin.tar.gz ovdas@192.168.1.6:/tmp/ovdas-install/

# 2. Importar en K3s
gunzip -c pgadmin.tar.gz | sudo k3s ctr images import -

# 3. Manifiesto: k8s/11-pgadmin.yaml
sudo k3s kubectl apply -f k8s/11-pgadmin.yaml
```

El manifiesto (`k8s/11-pgadmin.yaml`) incluye:
- `PersistentVolumeClaim` de 1Gi para la configuración de pgAdmin
- `Deployment` con `imagePullPolicy: IfNotPresent` (imagen local, sin internet)
- `Service` NodePort en el puerto **30500**
- Variables: `PGADMIN_CONFIG_SERVER_MODE=False` (sin login multi-usuario) y `PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED=False`

La conexión al servidor PostgreSQL está pre-configurada en `/var/lib/pgadmin/servers.json` dentro del pod.

---

## 10. Docker Compose (modo desarrollo)

Docker también está instalado en el servidor con todas las imágenes cargadas.

```bash
cd /ovdas/ovdasufro/ovdas-core

# Levantar todo
sudo docker-compose up -d

# Escalar un worker
sudo docker-compose up -d --scale worker-deteccion=3

# Ver logs
sudo docker-compose logs -f core

# Bajar todo
sudo docker-compose down -v
```

> `docker-compose` (binario v2.27.0) está en `/usr/local/bin/docker-compose`.

---

## 10. Comandos útiles de operación

### Ver estado general
```bash
sudo k3s kubectl -n ovdas get pods -o wide
sudo k3s kubectl -n ovdas get hpa
sudo k3s kubectl -n ovdas get svc
```

### Logs
```bash
sudo k3s kubectl -n ovdas logs -f deployment/core
sudo k3s kubectl -n ovdas logs -f deployment/worker-deteccion
sudo k3s kubectl -n ovdas logs -f statefulset/postgres
```

### Diagnóstico de un pod
```bash
sudo k3s kubectl -n ovdas describe pod <nombre-del-pod>
sudo k3s kubectl top nodes
sudo k3s kubectl -n ovdas top pods
```

### Test del pipeline completo
```bash
export KUBECONFIG=~/.kube/config
bash /ovdas/ovdasufro/ovdas-core/scripts/test-flow.sh k3s
```

### Acceso directo desde el servidor (sin tunnel)
```bash
curl http://localhost:30800/health         # Core
curl http://localhost:30808/health         # API Gateway

# Ingresar una traza manualmente
curl -X POST http://localhost:30800/ingesta/traza \
  -H "Content-Type: application/json" \
  -d '{"volcan_id":"VLL","estacion_id":"PFT","componente":"Z","duracion_seg":45.0,"muestra_hz":100}'
```

### Reiniciar un servicio
```bash
sudo k3s kubectl -n ovdas rollout restart deployment/core
sudo k3s kubectl -n ovdas rollout restart deployment/worker-deteccion
sudo k3s kubectl -n ovdas rollout status deployment/core
```

### Gestión de imágenes
```bash
sudo k3s ctr images list | grep ovdas   # imágenes en K3s
sudo docker images                       # imágenes en Docker

# Re-importar una imagen actualizada
gunzip -c /tmp/ovdas-install/app-images/core.tar.gz | sudo k3s ctr images import -
sudo k3s kubectl -n ovdas rollout restart deployment/core
```

### Gestión de K3s
```bash
sudo systemctl status k3s
sudo systemctl restart k3s
sudo journalctl -u k3s -f
/usr/local/bin/k3s-uninstall.sh   # desinstalar (¡cuidado!)
```

---

## 11. Archivos importantes

| Ruta (servidor remoto) | Descripción |
|---|---|
| `/ovdas/ovdasufro/ovdas-core/docker-compose.yml` | Composición Docker para desarrollo |
| `/ovdas/ovdasufro/ovdas-core/k8s/*.yaml` | 13 manifiestos Kubernetes |
| `/ovdas/ovdasufro/ovdas-core/k8s/00-seismic-pvc.yaml` | **Creado durante este despliegue** (faltaba en el repo) |
| `/ovdas/ovdasufro/ovdas-core/scripts/test-flow.sh` | Test end-to-end del pipeline |
| `/ovdas/ovdasufro/ovdas-core/scripts/deploy.sh` | Re-despliegue rápido |
| `/etc/systemd/system/docker.service` | Servicio Docker |
| `/etc/systemd/system/k3s.service` | Servicio K3s |
| `/etc/sudoers.d/ovdas-nopasswd` | NOPASSWD para usuario ovdas |
| `~/.kube/config` | Configuración kubectl |
| `/var/lib/rancher/k3s/storage/` | PVCs (datos persistentes de postgres, rabbitmq, seismic) |
| `/tmp/ovdas-install/` | Tarballs de instalación (pueden borrarse) |

---

## 12. Notas y advertencias

### wws-poller (modo dummy)
El servicio está configurado con `WWS_HOST: ""` en el manifiesto K8s → activa el **modo dummy**,
que genera trazas sintéticas en lugar de conectarse al servidor WWS real.

Para activar el WWS real cuando esté disponible:
```bash
sudo k3s kubectl -n ovdas set env deployment/wws-poller \
  WWS_HOST=<ip-del-servidor-wws> \
  WWS_PORT=29384
```

### Actualizar una imagen de aplicación
```bash
# 1. Máquina local — rebuild y guardar
cd /home/riley/Documents/ovdascla/ovdas-core
docker build -t ovdas/core:latest ./core
docker save ovdas/core:latest | gzip > /tmp/core-nuevo.tar.gz

# 2. Transferir
scp -P 6022 /tmp/core-nuevo.tar.gz ovdas@192.168.1.6:/tmp/

# 3. En el servidor — importar y reiniciar
gunzip -c /tmp/core-nuevo.tar.gz | sudo k3s ctr images import -
sudo k3s kubectl -n ovdas rollout restart deployment/core
```

### Docker y K3s tienen containerd separados
- `sudo docker images` → Docker daemon
- `sudo k3s ctr images list` → K3s containerd

Son **independientes**. Una imagen cargada en Docker no está automáticamente en K3s y viceversa.

### Persistencia de datos (PVCs)
Los datos persisten en `/var/lib/rancher/k3s/storage/` aunque los pods se reinicien.
Para hacer backup de PostgreSQL:
```bash
sudo k3s kubectl -n ovdas exec statefulset/postgres -- \
  pg_dump -U ovdas ovdas > /tmp/backup-ovdas.sql
```

### SSH ControlMaster (sesión persistente desde la máquina local)

El servidor y sus servicios (K3s, Docker, pods) **se autolevantan solos** al reiniciar el servidor
— están habilitados como servicios systemd con `restartPolicy: Always`.

Lo que **no** sobrevive un reinicio de tu PC local es el ControlMaster SSH y los tunnels.
Hay que reconectarlos manualmente:

```bash
# 1. Reconectar ControlMaster (necesario para usar la conexión SSH en Claude Code)
mkdir -p /tmp/ssh-ovdas
sshpass -p 'ovdas123' ssh \
  -o StrictHostKeyChecking=no \
  -o ControlMaster=yes \
  -o ControlPath=/tmp/ssh-ovdas/ctl \
  -o ControlPersist=yes \
  -p 6022 ovdas@192.168.1.6 -f -N

# 2. Levantar tunnels para acceder a los servicios desde el browser
ssh -L 8000:192.168.1.6:30800 \
    -L 8080:192.168.1.6:30808 \
    -L 15672:192.168.1.6:31672 \
    -L 8443:192.168.1.6:30900 \
    -p 6022 -N ovdas@192.168.1.6
```

```bash
# Verificar que el ControlMaster sigue activo:
ssh -o ControlPath=/tmp/ssh-ovdas/ctl -O check ovdas@192.168.1.6
```
